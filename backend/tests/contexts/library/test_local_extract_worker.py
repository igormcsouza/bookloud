from __future__ import annotations

import json

import pytest

from src.contexts.library.application.extraction import ExtractBook, ExtractBookResult
from src.contexts.library.interface import local_extract_worker
from src.contexts.library.interface.local_extract_worker import poll_once

QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/000000000000/bookloud-local-extract"


class StubUseCase:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.executed: list[str] = []

    def execute(self, command):
        if self.error is not None:
            raise self.error
        self.executed.append(command.book_id)
        return ExtractBookResult("EXTRACTED", chunks_written=1, page_count=1)


class FakeSqsClient:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages
        self.deleted_receipt_handles: list[str] = []
        self.receive_calls = 0

    def receive_message(self, *, QueueUrl: str, MaxNumberOfMessages: int, WaitTimeSeconds: int):
        self.receive_calls += 1
        return {"Messages": self._messages} if self._messages else {}

    def delete_message(self, *, QueueUrl: str, ReceiptHandle: str) -> None:
        self.deleted_receipt_handles.append(ReceiptHandle)


def _message(book_id: str, receipt_handle: str) -> dict:
    body = json.dumps(
        {
            "Records": [
                {
                    "eventName": "ObjectCreated:Put",
                    "s3": {
                        "bucket": {"name": "bookloud-local-pdfs"},
                        "object": {"key": f"books/user-1/{book_id}/source.pdf", "size": 10},
                    },
                }
            ]
        }
    )
    return {"Body": body, "ReceiptHandle": receipt_handle, "MessageId": f"msg-{receipt_handle}"}


def test_poll_once_no_messages_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_extract_worker, "_build_use_case", lambda: StubUseCase())
    sqs = FakeSqsClient([])
    assert poll_once(sqs, QUEUE_URL) == 0
    assert sqs.deleted_receipt_handles == []


def test_poll_once_processes_and_deletes_successful_message(monkeypatch: pytest.MonkeyPatch) -> None:
    use_case = StubUseCase()
    monkeypatch.setattr(local_extract_worker, "_build_use_case", lambda: use_case)
    sqs = FakeSqsClient([_message("book-1", "rh-1")])

    processed = poll_once(sqs, QUEUE_URL)

    assert processed == 1
    assert sqs.deleted_receipt_handles == ["rh-1"]
    assert use_case.executed == ["book-1"]


def test_poll_once_leaves_failed_message_undeleted_for_redelivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_case = StubUseCase(error=RuntimeError("transient S3 error"))
    monkeypatch.setattr(local_extract_worker, "_build_use_case", lambda: use_case)
    sqs = FakeSqsClient([_message("book-1", "rh-1")])

    processed = poll_once(sqs, QUEUE_URL)

    assert processed == 0
    assert sqs.deleted_receipt_handles == []


def test_poll_once_processes_each_message_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    """One message succeeds, one fails -- each gets a fresh use case (a new
    ``_build_use_case()`` call per message), and only the successful one is
    deleted."""
    calls: list[StubUseCase] = []

    def build():
        # First message succeeds, second fails.
        use_case = StubUseCase(error=RuntimeError("boom") if len(calls) == 1 else None)
        calls.append(use_case)
        return use_case

    monkeypatch.setattr(local_extract_worker, "_build_use_case", build)
    sqs = FakeSqsClient([_message("book-1", "rh-1"), _message("book-2", "rh-2")])

    processed = poll_once(sqs, QUEUE_URL)

    assert processed == 1
    assert sqs.deleted_receipt_handles == ["rh-1"]
    assert len(calls) == 2


def test_build_use_case_returns_a_real_extract_book(dynamodb_table) -> None:
    """Exercises the real (non-monkeypatched) composition root -- the
    `dynamodb_table` fixture just keeps `settings.table_name` pointed at a
    real (moto) table so building the repositories doesn't touch real AWS."""
    use_case = local_extract_worker._build_use_case()
    assert isinstance(use_case, ExtractBook)
