from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime

import boto3
import pytest

from src.contexts.library.application.extraction import ExtractBookResult
from src.contexts.library.domain.book import Book
from src.contexts.library.domain.value_objects import BookStatus
from src.contexts.library.infrastructure.s3_keys import source_pdf_key
from src.contexts.library.interface.extract_handler import (
    handle_records,
    handler,
    s3_objects_from_sqs_body,
    scheduled_handler,
)
from tests.contexts.library.pdf_fixtures import simple_text_pdf

USER_ID = "user-1"
BOOK_ID = "book-1"
PDF_BUCKET = "bookloud-test-pdfs"


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


# --- s3_objects_from_sqs_body: parsing edge cases -------------------------------


def test_s3_objects_from_sqs_body_parses_object_created() -> None:
    body = _s3_event_body("my-bucket", "books/u/b/source.pdf", size=42)
    objects = s3_objects_from_sqs_body(body)
    assert objects == [("my-bucket", "books/u/b/source.pdf", 42)]


def test_s3_objects_from_sqs_body_url_decodes_the_key() -> None:
    body = _s3_event_body("my-bucket", "books/u ser/b/source.pdf")
    objects = s3_objects_from_sqs_body(body)
    assert objects == [("my-bucket", "books/u ser/b/source.pdf", 123)]


def test_s3_objects_from_sqs_body_ignores_s3_test_event() -> None:
    body = json.dumps({"Service": "Amazon S3", "Event": "s3:TestEvent", "Bucket": "my-bucket"})
    assert s3_objects_from_sqs_body(body) == []


def test_s3_objects_from_sqs_body_ignores_non_object_created_events() -> None:
    body = json.dumps(
        {
            "Records": [
                {
                    "eventName": "ObjectRemoved:Delete",
                    "s3": {"bucket": {"name": "b"}, "object": {"key": "books/u/b/source.pdf"}},
                }
            ]
        }
    )
    assert s3_objects_from_sqs_body(body) == []


def test_s3_objects_from_sqs_body_ignores_malformed_json() -> None:
    assert s3_objects_from_sqs_body("not json at all") == []


def test_s3_objects_from_sqs_body_ignores_body_with_no_records() -> None:
    assert s3_objects_from_sqs_body(json.dumps({"foo": "bar"})) == []


def test_s3_objects_from_sqs_body_ignores_record_missing_bucket_or_key() -> None:
    body = json.dumps(
        {
            "Records": [
                {"eventName": "ObjectCreated:Put", "s3": {"bucket": {"name": ""}, "object": {"key": "books/a/b/source.pdf"}}},
                {"eventName": "ObjectCreated:Put", "s3": {"bucket": {"name": "b"}, "object": {"key": ""}}},
            ]
        }
    )
    assert s3_objects_from_sqs_body(body) == []


def test_s3_objects_from_sqs_body_handles_multiple_records() -> None:
    body = json.dumps(
        {
            "Records": [
                {
                    "eventName": "ObjectCreated:Post",
                    "s3": {"bucket": {"name": "b1"}, "object": {"key": "books/a/1/source.pdf", "size": 10}},
                },
                {
                    "eventName": "ObjectCreated:Put",
                    "s3": {"bucket": {"name": "b2"}, "object": {"key": "books/b/2/source.pdf", "size": 20}},
                },
            ]
        }
    )
    objects = s3_objects_from_sqs_body(body)
    assert objects == [("b1", "books/a/1/source.pdf", 10), ("b2", "books/b/2/source.pdf", 20)]


# --- handle_records: routing to the use case -------------------------------------


class StubUseCase:
    def __init__(self) -> None:
        self.commands = []

    def execute(self, command):
        self.commands.append(command)
        return ExtractBookResult("EXTRACTED", chunks_written=1, page_count=1)


def test_handle_records_routes_valid_keys_to_the_use_case() -> None:
    use_case = StubUseCase()
    records = [{"body": _s3_event_body(PDF_BUCKET, source_pdf_key(USER_ID, BOOK_ID))}]

    results = handle_records(records, use_case)

    assert len(results) == 1
    assert results[0].outcome == "EXTRACTED"
    assert len(use_case.commands) == 1
    command = use_case.commands[0]
    assert command.user_id == USER_ID
    assert command.book_id == BOOK_ID
    assert command.source_key == source_pdf_key(USER_ID, BOOK_ID)


def test_handle_records_skips_unrecognized_keys() -> None:
    use_case = StubUseCase()
    records = [{"body": _s3_event_body(PDF_BUCKET, "not-a-books-key.pdf")}]

    results = handle_records(records, use_case)

    assert results == []
    assert use_case.commands == []


def test_handle_records_ignores_test_event_body() -> None:
    use_case = StubUseCase()
    records = [{"body": json.dumps({"Event": "s3:TestEvent"})}]

    results = handle_records(records, use_case)

    assert results == []
    assert use_case.commands == []


def test_handle_records_processes_a_batch_of_two() -> None:
    use_case = StubUseCase()
    records = [
        {"body": _s3_event_body(PDF_BUCKET, source_pdf_key("user-a", "book-a"))},
        {"body": _s3_event_body(PDF_BUCKET, source_pdf_key("user-b", "book-b"))},
    ]

    results = handle_records(records, use_case)

    assert len(results) == 2
    assert {c.book_id for c in use_case.commands} == {"book-a", "book-b"}


def test_handle_records_empty_records_returns_empty_list() -> None:
    assert handle_records([], StubUseCase()) == []


# --- handler(): full composition root + real generated PDF, end to end ----------


@pytest.fixture
def s3_and_dynamodb(dynamodb_table, monkeypatch: pytest.MonkeyPatch):
    import src.config as config

    monkeypatch.setattr(config.settings, "pdf_bucket", PDF_BUCKET)
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=PDF_BUCKET)

    # The extract Lambda is now a SynthesisQueue producer too (PLANS/
    # phase-4.md §4) -- handler() reaches EXTRACTED and publishes the
    # fan-out, so this fixture needs a real (moto) queue for that call to
    # land on.
    sqs = boto3.client("sqs", region_name="us-east-1")
    queue_url = sqs.create_queue(QueueName="bookloud-test-synthesize")["QueueUrl"]
    monkeypatch.setattr(config.settings, "synthesize_queue_url", queue_url)

    return client


def test_handler_end_to_end_extracts_a_real_pdf_and_writes_chunks(
    s3_and_dynamodb, dynamodb_table
) -> None:
    from src.contexts.library.infrastructure.dynamodb_book_repository import (
        DynamoDbBookRepository,
    )
    from src.contexts.library.infrastructure.dynamodb_chunk_repository import (
        DynamoDbChunkRepository,
    )
    from src.infrastructure.clock import SystemClock

    key = source_pdf_key(USER_ID, BOOK_ID)
    book_repo = DynamoDbBookRepository(table=dynamodb_table)
    book = Book.create(
        id=BOOK_ID, user_id=USER_ID, title_raw="Real Book", now=SystemClock().now(), source_key=key
    )
    book_repo.save(book)
    s3_and_dynamodb.put_object(Bucket=PDF_BUCKET, Key=key, Body=simple_text_pdf())

    event = {"Records": [{"body": _s3_event_body(PDF_BUCKET, key)}]}
    handler(event, None)

    updated = book_repo.get(USER_ID, BOOK_ID)
    assert updated.status == BookStatus.EXTRACTED
    assert updated.chunks_total >= 1

    chunk_repo = DynamoDbChunkRepository(table=dynamodb_table)
    chunks = chunk_repo.list_for_book(BOOK_ID)
    assert len(chunks) == updated.chunks_total
    assert "simple test document" in chunks[0].text


def test_handler_end_to_end_publishes_synthesis_fan_out(s3_and_dynamodb, dynamodb_table) -> None:
    """PLANS/phase-4.md §4: the extract Lambda publishes one SQS message per
    chunk after the EXTRACTED flip -- proven here against a real (moto) SQS
    queue, not a fake."""
    import src.config as config
    from src.contexts.library.infrastructure.dynamodb_book_repository import (
        DynamoDbBookRepository,
    )

    key = source_pdf_key(USER_ID, BOOK_ID)
    book_repo = DynamoDbBookRepository(table=dynamodb_table)
    book = Book.create(
        id=BOOK_ID, user_id=USER_ID, title_raw="Real Book", now=datetime.now(UTC), source_key=key
    )
    book_repo.save(book)
    s3_and_dynamodb.put_object(Bucket=PDF_BUCKET, Key=key, Body=simple_text_pdf())

    event = {"Records": [{"body": _s3_event_body(PDF_BUCKET, key)}]}
    handler(event, None)

    updated = book_repo.get(USER_ID, BOOK_ID)
    sqs = boto3.client("sqs", region_name="us-east-1")
    response = sqs.receive_message(
        QueueUrl=config.settings.synthesize_queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0
    )
    messages = response.get("Messages", [])
    assert len(messages) == updated.chunks_total
    bodies = [json.loads(m["Body"]) for m in messages]
    assert {b["chunkIndex"] for b in bodies} == set(range(updated.chunks_total))
    assert all(b["bookId"] == BOOK_ID for b in bodies)


def test_handler_permanent_failure_flips_book_to_failed(s3_and_dynamodb, dynamodb_table) -> None:
    from src.contexts.library.infrastructure.dynamodb_book_repository import (
        DynamoDbBookRepository,
    )
    from tests.contexts.library.pdf_fixtures import CORRUPT_PDF_BYTES

    key = source_pdf_key(USER_ID, BOOK_ID)
    book_repo = DynamoDbBookRepository(table=dynamodb_table)
    book = Book.create(id=BOOK_ID, user_id=USER_ID, title_raw="Bad Book", now=datetime.now(UTC), source_key=key)
    book_repo.save(book)
    s3_and_dynamodb.put_object(Bucket=PDF_BUCKET, Key=key, Body=CORRUPT_PDF_BYTES)

    event = {"Records": [{"body": _s3_event_body(PDF_BUCKET, key)}]}
    handler(event, None)

    updated = book_repo.get(USER_ID, BOOK_ID)
    assert updated.status == BookStatus.FAILED
    assert updated.failure_reason == "CORRUPT_PDF"


# --- scheduled_handler(): the EventBridge-Rule-triggered entrypoint --------------


class _FakeLambdaContext:
    def get_remaining_time_in_millis(self) -> int:
        return 60_000


def test_scheduled_handler_drains_the_extract_queue_and_deletes_the_message(
    s3_and_dynamodb, dynamodb_table, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scheduled_handler (the deployed cmd, replacing the SqsEventSource) must
    poll settings.extract_queue_url itself, process whatever's there through
    the exact same use case as handler(), and delete the message on
    success -- nothing left for the next scheduled tick to reprocess."""
    import src.config as config
    from src.contexts.library.infrastructure.dynamodb_book_repository import (
        DynamoDbBookRepository,
    )

    key = source_pdf_key(USER_ID, BOOK_ID)
    book_repo = DynamoDbBookRepository(table=dynamodb_table)
    book = Book.create(
        id=BOOK_ID, user_id=USER_ID, title_raw="Real Book", now=datetime.now(UTC), source_key=key
    )
    book_repo.save(book)
    s3_and_dynamodb.put_object(Bucket=PDF_BUCKET, Key=key, Body=simple_text_pdf())

    sqs = boto3.client("sqs", region_name="us-east-1")
    queue_url = sqs.create_queue(QueueName="bookloud-test-extract")["QueueUrl"]
    monkeypatch.setattr(config.settings, "extract_queue_url", queue_url)
    sqs.send_message(QueueUrl=queue_url, MessageBody=_s3_event_body(PDF_BUCKET, key))

    scheduled_handler({}, _FakeLambdaContext())

    updated = book_repo.get(USER_ID, BOOK_ID)
    assert updated.status == BookStatus.EXTRACTED

    remaining = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0)
    assert remaining.get("Messages", []) == []
