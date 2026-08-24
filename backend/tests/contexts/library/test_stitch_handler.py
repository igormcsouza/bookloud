from __future__ import annotations

import json

import boto3
import pytest

from src.contexts.library.application.stitching import StitchBookResult
from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.value_objects import BookStatus, ChunkStatus
from src.contexts.library.infrastructure.s3_keys import book_manifest_key
from src.contexts.library.infrastructure.sqs_stitch_queue import STITCH_MESSAGE_VERSION
from src.contexts.library.interface.stitch_handler import (
    command_from_record,
    handle_records,
    handler,
    scheduled_handler,
)

USER_ID = "user-1"
BOOK_ID = "book-1"
AUDIO_BUCKET = "bookloud-test-audio"
MARKS_BUCKET = "bookloud-test-marks"


def _body(user_id: str = USER_ID, book_id: str = BOOK_ID, v: int = STITCH_MESSAGE_VERSION) -> str:
    return json.dumps({"v": v, "userId": user_id, "bookId": book_id})


def _record(body: str, *, receive_count: str | None = None) -> dict:
    record: dict = {"body": body}
    if receive_count is not None:
        record["attributes"] = {"ApproximateReceiveCount": receive_count}
    return record


# --- command_from_record: parsing edge cases --------------------------------


def test_command_from_record_parses_a_valid_body() -> None:
    command = command_from_record(_record(_body(), receive_count="2"))
    assert command is not None
    assert command.user_id == USER_ID
    assert command.book_id == BOOK_ID
    assert command.attempt == 2


def test_command_from_record_defaults_attempt_to_1_when_attributes_missing() -> None:
    command = command_from_record(_record(_body()))
    assert command is not None
    assert command.attempt == 1


def test_command_from_record_malformed_json_returns_none() -> None:
    assert command_from_record(_record("not json at all")) is None


def test_command_from_record_unknown_version_returns_none() -> None:
    assert command_from_record(_record(_body(v=99))) is None


def test_command_from_record_missing_v_returns_none() -> None:
    assert command_from_record(_record(json.dumps({"userId": "u", "bookId": "b"}))) is None


@pytest.mark.parametrize("payload", [{"v": 1, "userId": "u"}, {"v": 1, "bookId": "b"}])
def test_command_from_record_missing_required_field_returns_none(payload: dict) -> None:
    assert command_from_record(_record(json.dumps(payload))) is None


def test_command_from_record_non_dict_json_returns_none() -> None:
    assert command_from_record(_record(json.dumps([1, 2, 3]))) is None


# --- handle_records ---------------------------------------------------------


class StubUseCase:
    def __init__(self) -> None:
        self.commands = []

    def execute(self, command):
        self.commands.append(command)
        return StitchBookResult("READY", segments=2, duration_ms=1000)


def test_handle_records_routes_valid_records_to_the_use_case() -> None:
    use_case = StubUseCase()
    results = handle_records([_record(_body())], use_case)
    assert len(results) == 1
    assert results[0].outcome == "READY"
    assert use_case.commands[0].book_id == BOOK_ID


def test_handle_records_skips_unparseable_records() -> None:
    use_case = StubUseCase()
    assert handle_records([_record("garbage")], use_case) == []
    assert use_case.commands == []


def test_handle_records_empty_returns_empty_list() -> None:
    assert handle_records([], StubUseCase()) == []


# --- handler(): full composition root against moto ---------------------------


@pytest.fixture
def stitch_infra(dynamodb_table, monkeypatch: pytest.MonkeyPatch):
    import src.config as config

    monkeypatch.setattr(config.settings, "audio_bucket", AUDIO_BUCKET)
    monkeypatch.setattr(config.settings, "marks_bucket", MARKS_BUCKET)
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=AUDIO_BUCKET)
    client.create_bucket(Bucket=MARKS_BUCKET)
    return client


def test_handler_end_to_end_all_failed_book_reaches_partial_no_audio(
    stitch_infra, dynamodb_table
) -> None:
    """The deterministic non-prod outcome (PLANS/phase-5.md §3.1), proven
    through the real composition root against real (moto) DynamoDB and S3:
    a book whose every chunk failed synthesis becomes PARTIAL/NO_AUDIO with a
    real book.json -- and never FAILED."""
    from src.contexts.library.infrastructure.dynamodb_book_repository import (
        DynamoDbBookRepository,
    )
    from src.contexts.library.infrastructure.dynamodb_chunk_repository import (
        DynamoDbChunkRepository,
    )
    from src.infrastructure.clock import SystemClock

    book_repo = DynamoDbBookRepository(table=dynamodb_table)
    chunk_repo = DynamoDbChunkRepository(table=dynamodb_table)

    book = Book.create(id=BOOK_ID, user_id=USER_ID, title_raw="Real Book", now=SystemClock().now())
    book.status = BookStatus.EXTRACTED
    book.chunks_total = 2
    book.chunks_done = 2
    book.chunks_failed = 2
    book_repo.save(book)
    for index in range(2):
        chunk_repo.save(
            Chunk(
                book_id=BOOK_ID,
                index=index,
                user_id=USER_ID,
                text=f"chunk {index}",
                char_start=index * 10,
                char_end=(index + 1) * 10,
                audio_key=None,
                marks_key=None,
                status=ChunkStatus.FAILED,
            )
        )

    handler({"Records": [_record(_body(), receive_count="1")]}, None)

    updated = book_repo.get(USER_ID, BOOK_ID)
    assert updated.status is BookStatus.PARTIAL
    assert updated.failure_reason == "NO_AUDIO"
    assert updated.audio_key is None
    assert updated.audio_duration_ms == 0
    assert updated.manifest_key == book_manifest_key(USER_ID, BOOK_ID)

    stored = stitch_infra.get_object(Bucket=MARKS_BUCKET, Key=updated.manifest_key)["Body"].read()
    document = json.loads(stored)
    assert document["segments"] == []
    assert document["missing"] == [0, 1]
    assert document["status"] == "PARTIAL"


# --- scheduled_handler(): the EventBridge-Rule-triggered entrypoint --------------


class _FakeLambdaContext:
    def get_remaining_time_in_millis(self) -> int:
        return 60_000


def test_scheduled_handler_drains_the_stitch_queue_and_deletes_the_message(
    stitch_infra, dynamodb_table, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scheduled_handler (the deployed cmd, replacing the SqsEventSource) must
    poll settings.stitch_queue_url itself, process whatever's there through
    the exact same use case as handler(), and delete the message on
    success -- nothing left for the next scheduled tick to reprocess."""
    import src.config as config
    from src.contexts.library.infrastructure.dynamodb_book_repository import (
        DynamoDbBookRepository,
    )
    from src.contexts.library.infrastructure.dynamodb_chunk_repository import (
        DynamoDbChunkRepository,
    )
    from src.infrastructure.clock import SystemClock

    book_repo = DynamoDbBookRepository(table=dynamodb_table)
    chunk_repo = DynamoDbChunkRepository(table=dynamodb_table)

    book = Book.create(id=BOOK_ID, user_id=USER_ID, title_raw="Real Book", now=SystemClock().now())
    book.status = BookStatus.EXTRACTED
    book.chunks_total = 2
    book.chunks_done = 2
    book.chunks_failed = 2
    book_repo.save(book)
    for index in range(2):
        chunk_repo.save(
            Chunk(
                book_id=BOOK_ID,
                index=index,
                user_id=USER_ID,
                text=f"chunk {index}",
                char_start=index * 10,
                char_end=(index + 1) * 10,
                audio_key=None,
                marks_key=None,
                status=ChunkStatus.FAILED,
            )
        )

    sqs = boto3.client("sqs", region_name="us-east-1")
    queue_url = sqs.create_queue(QueueName="bookloud-test-stitch")["QueueUrl"]
    monkeypatch.setattr(config.settings, "stitch_queue_url", queue_url)
    sqs.send_message(QueueUrl=queue_url, MessageBody=_body())

    scheduled_handler({}, _FakeLambdaContext())

    updated = book_repo.get(USER_ID, BOOK_ID)
    assert updated.status is BookStatus.PARTIAL

    remaining = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0)
    assert remaining.get("Messages", []) == []
