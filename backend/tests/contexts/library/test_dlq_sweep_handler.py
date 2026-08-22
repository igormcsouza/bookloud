from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime

import boto3
import pytest

from src.contexts.library.application.sweeping import SweepDlqResult
from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.value_objects import BookStatus, ChunkStatus, ExtractionFailure
from src.contexts.library.infrastructure.s3_keys import source_pdf_key
from src.contexts.library.infrastructure.sqs_synthesis_queue import SYNTHESIS_MESSAGE_VERSION
from src.contexts.library.interface.dlq_sweep_handler import handle_records, handler

USER_ID = "user-1"
BOOK_ID = "book-1"


def _s3_event_body(bucket: str, key: str, size: int = 123) -> str:
    return json.dumps(
        {
            "Records": [
                {
                    "eventName": "ObjectCreated:Put",
                    "s3": {
                        "bucket": {"name": bucket},
                        "object": {"key": urllib.parse.quote(key), "size": size},
                    },
                }
            ]
        }
    )


def _synthesize_body(user_id: str = USER_ID, book_id: str = BOOK_ID, chunk_index: int = 0) -> str:
    return json.dumps(
        {"v": SYNTHESIS_MESSAGE_VERSION, "userId": user_id, "bookId": book_id, "chunkIndex": chunk_index}
    )


def _extract_dlq_record(body: str) -> dict:
    return {"body": body, "eventSourceARN": "arn:aws:sqs:us-east-1:111111111111:bookloud-pr-1-extract-dlq"}


def _synthesize_dlq_record(body: str) -> dict:
    return {"body": body, "eventSourceARN": "arn:aws:sqs:us-east-1:111111111111:bookloud-pr-1-synthesize-dlq"}


# --- handle_records: routing by eventSourceARN ------------------------------


class StubUseCase:
    def __init__(self) -> None:
        self.extract_commands: list = []
        self.synthesize_commands: list = []

    def sweep_extract(self, command):
        self.extract_commands.append(command)
        return SweepDlqResult("FAILED", reason="DLQ_EXHAUSTED")

    def sweep_synthesize(self, command):
        self.synthesize_commands.append(command)
        return SweepDlqResult("STITCH_REQUEUED")


def test_handle_records_routes_extract_dlq_record() -> None:
    use_case = StubUseCase()
    body = _s3_event_body("bookloud-pdfs", source_pdf_key(USER_ID, BOOK_ID))
    results = handle_records([_extract_dlq_record(body)], use_case)

    assert len(results) == 1
    assert results[0].outcome == "FAILED"
    assert len(use_case.extract_commands) == 1
    assert use_case.extract_commands[0].user_id == USER_ID
    assert use_case.extract_commands[0].book_id == BOOK_ID
    assert use_case.synthesize_commands == []


def test_handle_records_routes_synthesize_dlq_record() -> None:
    use_case = StubUseCase()
    results = handle_records([_synthesize_dlq_record(_synthesize_body(chunk_index=3))], use_case)

    assert len(results) == 1
    assert results[0].outcome == "STITCH_REQUEUED"
    assert use_case.extract_commands == []
    assert len(use_case.synthesize_commands) == 1
    assert use_case.synthesize_commands[0].chunk_index == 3


def test_handle_records_ignores_unrecognized_event_source_arn() -> None:
    use_case = StubUseCase()
    record = {"body": "{}", "eventSourceARN": "arn:aws:sqs:us-east-1:111111111111:bookloud-pr-1-stitch-dlq"}
    assert handle_records([record], use_case) == []
    assert use_case.extract_commands == []
    assert use_case.synthesize_commands == []


def test_handle_records_extract_ignores_unrecognized_key() -> None:
    use_case = StubUseCase()
    body = _s3_event_body("bookloud-pdfs", "not/a/recognized/key.pdf")
    assert handle_records([_extract_dlq_record(body)], use_case) == []
    assert use_case.extract_commands == []


def test_handle_records_extract_ignores_s3_test_event() -> None:
    use_case = StubUseCase()
    body = json.dumps({"Service": "Amazon S3", "Event": "s3:TestEvent", "Bucket": "b"})
    assert handle_records([_extract_dlq_record(body)], use_case) == []


def test_handle_records_synthesize_ignores_malformed_body() -> None:
    use_case = StubUseCase()
    assert handle_records([_synthesize_dlq_record("garbage")], use_case) == []
    assert use_case.synthesize_commands == []


def test_handle_records_empty_returns_empty_list() -> None:
    assert handle_records([], StubUseCase()) == []


def test_handle_records_batch_of_both_kinds() -> None:
    use_case = StubUseCase()
    extract_body = _s3_event_body("bookloud-pdfs", source_pdf_key(USER_ID, BOOK_ID))
    records = [_extract_dlq_record(extract_body), _synthesize_dlq_record(_synthesize_body())]

    results = handle_records(records, use_case)

    assert len(results) == 2
    assert len(use_case.extract_commands) == 1
    assert len(use_case.synthesize_commands) == 1


# --- handler(): full composition root against moto --------------------------


@pytest.fixture
def sweep_infra(dynamodb_table, monkeypatch: pytest.MonkeyPatch):
    import src.config as config

    sqs = boto3.client("sqs", region_name="us-east-1")
    queue_url = sqs.create_queue(QueueName="bookloud-test-stitch")["QueueUrl"]
    monkeypatch.setattr(config.settings, "stitch_queue_url", queue_url)
    return sqs


def test_handler_end_to_end_marks_stranded_extraction_book_failed(sweep_infra, dynamodb_table) -> None:
    from src.contexts.library.infrastructure.dynamodb_book_repository import DynamoDbBookRepository
    from src.infrastructure.clock import SystemClock

    book_repo = DynamoDbBookRepository(table=dynamodb_table)
    book = Book.create(id=BOOK_ID, user_id=USER_ID, title_raw="Stranded Book", now=SystemClock().now())
    book.source_key = source_pdf_key(USER_ID, BOOK_ID)
    book_repo.save(book)

    body = _s3_event_body("bookloud-pdfs", source_pdf_key(USER_ID, BOOK_ID))
    handler({"Records": [_extract_dlq_record(body)]}, None)

    updated = book_repo.get(USER_ID, BOOK_ID)
    assert updated.status is BookStatus.FAILED
    assert updated.failure_reason == ExtractionFailure.DLQ_EXHAUSTED.value


def test_handler_end_to_end_finalizes_stranded_chunk_and_requeues_stitch(sweep_infra, dynamodb_table) -> None:
    import src.config as config
    from src.contexts.library.infrastructure.dynamodb_book_repository import DynamoDbBookRepository
    from src.contexts.library.infrastructure.dynamodb_chunk_repository import DynamoDbChunkRepository
    from src.infrastructure.clock import SystemClock

    book_repo = DynamoDbBookRepository(table=dynamodb_table)
    chunk_repo = DynamoDbChunkRepository(table=dynamodb_table)

    book = Book.create(id=BOOK_ID, user_id=USER_ID, title_raw="Real Book", now=SystemClock().now())
    book.status = BookStatus.EXTRACTED
    book.chunks_total = 1
    book_repo.save(book)
    chunk_repo.save(
        Chunk(
            book_id=BOOK_ID,
            index=0,
            user_id=USER_ID,
            text="chunk text",
            char_start=0,
            char_end=10,
            audio_key=None,
            marks_key=None,
            status=ChunkStatus.PENDING,
        )
    )

    handler({"Records": [_synthesize_dlq_record(_synthesize_body())]}, None)

    updated_chunk = chunk_repo.get(BOOK_ID, 0)
    assert updated_chunk.status == ChunkStatus.FAILED

    updated_book = book_repo.get(USER_ID, BOOK_ID)
    assert updated_book.chunks_done == 1
    assert updated_book.chunks_failed == 1

    messages = boto3.client("sqs", region_name="us-east-1").receive_message(
        QueueUrl=config.settings.stitch_queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0
    ).get("Messages", [])
    assert len(messages) == 1
    assert json.loads(messages[0]["Body"]) == {"v": 1, "userId": USER_ID, "bookId": BOOK_ID}
