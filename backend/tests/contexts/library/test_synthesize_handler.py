from __future__ import annotations

import json

import boto3
import pytest

from src.contexts.library.application.synthesis import SynthesizeChunkResult
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.value_objects import ChunkStatus
from src.contexts.library.infrastructure.sqs_synthesis_queue import SYNTHESIS_MESSAGE_VERSION
from src.contexts.library.interface.synthesize_handler import command_from_record, handle_records, handler

USER_ID = "user-1"
BOOK_ID = "book-1"
AUDIO_BUCKET = "bookloud-test-audio"
MARKS_BUCKET = "bookloud-test-marks"


def _body(user_id: str = USER_ID, book_id: str = BOOK_ID, chunk_index: int = 0, v: int = SYNTHESIS_MESSAGE_VERSION) -> str:
    return json.dumps({"v": v, "userId": user_id, "bookId": book_id, "chunkIndex": chunk_index})


def _record(body: str, *, receive_count: str | None = None) -> dict:
    record = {"body": body}
    if receive_count is not None:
        record["attributes"] = {"ApproximateReceiveCount": receive_count}
    return record


# --- command_from_record: parsing edge cases --------------------------------


def test_command_from_record_parses_valid_body() -> None:
    command = command_from_record(_record(_body(chunk_index=7), receive_count="3"))
    assert command is not None
    assert command.user_id == USER_ID
    assert command.book_id == BOOK_ID
    assert command.chunk_index == 7
    assert command.attempt == 3


def test_command_from_record_defaults_attempt_to_1_when_attributes_missing() -> None:
    command = command_from_record(_record(_body()))
    assert command is not None
    assert command.attempt == 1


def test_command_from_record_malformed_json_returns_none() -> None:
    assert command_from_record(_record("not json at all")) is None


def test_command_from_record_unknown_version_returns_none() -> None:
    assert command_from_record(_record(_body(v=99))) is None


def test_command_from_record_missing_v_returns_none() -> None:
    assert command_from_record(_record(json.dumps({"userId": "u", "bookId": "b", "chunkIndex": 0}))) is None


def test_command_from_record_missing_required_field_returns_none() -> None:
    assert command_from_record(_record(json.dumps({"v": 1, "userId": "u"}))) is None


def test_command_from_record_non_dict_json_returns_none() -> None:
    assert command_from_record(_record(json.dumps([1, 2, 3]))) is None


def test_command_from_record_non_integer_chunk_index_returns_none() -> None:
    assert command_from_record(_record(json.dumps({"v": 1, "userId": "u", "bookId": "b", "chunkIndex": "abc"}))) is None


# --- handle_records: routing to the use case, batch handling ---------------


class StubUseCase:
    def __init__(self) -> None:
        self.commands = []

    def execute(self, command):
        self.commands.append(command)
        return SynthesizeChunkResult("DONE", duration_ms=1000, source="edge-tts", chunks_done=1, chunks_total=1)


def test_handle_records_routes_valid_records_to_the_use_case() -> None:
    use_case = StubUseCase()
    records = [_record(_body(chunk_index=3))]

    results = handle_records(records, use_case)

    assert len(results) == 1
    assert results[0].outcome == "DONE"
    assert use_case.commands[0].chunk_index == 3


def test_handle_records_skips_unparseable_records() -> None:
    use_case = StubUseCase()
    results = handle_records([_record("garbage")], use_case)
    assert results == []
    assert use_case.commands == []


def test_handle_records_batch_of_two() -> None:
    use_case = StubUseCase()
    records = [_record(_body(chunk_index=0)), _record(_body(chunk_index=1))]

    results = handle_records(records, use_case)

    assert len(results) == 2
    assert [c.chunk_index for c in use_case.commands] == [0, 1]


def test_handle_records_empty_returns_empty_list() -> None:
    assert handle_records([], StubUseCase()) == []


# --- handler(): full composition root + real repos/storage, end to end -----


@pytest.fixture
def synthesis_infra(dynamodb_table, monkeypatch: pytest.MonkeyPatch):
    import src.config as config

    monkeypatch.setattr(config.settings, "audio_bucket", AUDIO_BUCKET)
    monkeypatch.setattr(config.settings, "marks_bucket", MARKS_BUCKET)
    monkeypatch.setattr(config.settings, "environment", "local")  # -> StubSynthesizer (§0)
    monkeypatch.setattr(config.settings, "synthesis_stub_mode", "disabled")
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=AUDIO_BUCKET)
    client.create_bucket(Bucket=MARKS_BUCKET)
    # Phase 5: the completing increment publishes a stitch message, so the
    # composition root needs a real (moto) queue behind get_stitch_queue().
    sqs = boto3.client("sqs", region_name="us-east-1")
    queue_url = sqs.create_queue(QueueName="bookloud-test-stitch")["QueueUrl"]
    monkeypatch.setattr(config.settings, "stitch_queue_url", queue_url)
    return client


def test_handler_end_to_end_stub_environment_fails_chunk_deterministically(synthesis_infra, dynamodb_table) -> None:
    """In local/test environments get_speech_synthesizer() returns a
    StubSynthesizer (PLANS/phase-4.md §0) -- this proves the full handler ->
    use case -> repository wiring end to end against real (moto) DynamoDB,
    landing on the deterministic FAILED/EXTERNAL_TTS_DISABLED outcome."""
    from src.contexts.library.infrastructure.dynamodb_book_repository import (
        DynamoDbBookRepository,
    )
    from src.contexts.library.infrastructure.dynamodb_chunk_repository import (
        DynamoDbChunkRepository,
    )

    book_repo = DynamoDbBookRepository(table=dynamodb_table)
    chunk_repo = DynamoDbChunkRepository(table=dynamodb_table)

    from src.contexts.library.domain.book import Book
    from src.infrastructure.clock import SystemClock

    book = Book.create(id=BOOK_ID, user_id=USER_ID, title_raw="Real Book", now=SystemClock().now())
    book.chunks_total = 1
    book_repo.save(book)
    chunk_repo.save(
        Chunk(
            book_id=BOOK_ID,
            index=0,
            user_id=USER_ID,
            text="Some real chunk text to synthesize.",
            char_start=0,
            char_end=36,
            audio_key=None,
            marks_key=None,
            status=ChunkStatus.PENDING,
        )
    )

    event = {"Records": [_record(_body(chunk_index=0), receive_count="1")]}
    handler(event, None)

    updated_chunk = chunk_repo.get(BOOK_ID, 0)
    assert updated_chunk.status == ChunkStatus.FAILED
    assert updated_chunk.failure_reason == "EXTERNAL_TTS_DISABLED"
    assert updated_chunk.audio_key is None
    assert updated_chunk.marks_key is None

    updated_book = book_repo.get(USER_ID, BOOK_ID)
    assert updated_book.chunks_done == 1
    assert updated_book.chunks_failed == 1


def test_handler_publishes_the_stitch_message_on_the_completing_increment(
    synthesis_infra, dynamodb_table
) -> None:
    """The phase-5 fan-in edge, proven through the real composition root:
    even though the chunk FAILED (stub environment), the increment that
    completes the book publishes exactly one stitch message."""
    import src.config as config
    from src.contexts.library.domain.book import Book
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
    book.chunks_total = 1
    book_repo.save(book)
    chunk_repo.save(
        Chunk(
            book_id=BOOK_ID,
            index=0,
            user_id=USER_ID,
            text="Some real chunk text to synthesize.",
            char_start=0,
            char_end=36,
            audio_key=None,
            marks_key=None,
            status=ChunkStatus.PENDING,
        )
    )

    handler({"Records": [_record(_body(chunk_index=0), receive_count="1")]}, None)

    sqs = boto3.client("sqs", region_name="us-east-1")
    messages = sqs.receive_message(
        QueueUrl=config.settings.stitch_queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0
    ).get("Messages", [])
    assert len(messages) == 1
    assert json.loads(messages[0]["Body"]) == {"v": 1, "userId": USER_ID, "bookId": BOOK_ID}
