from __future__ import annotations

import json

import pytest

from src.contexts.library.application.stitching import StitchBookResult
from src.contexts.library.infrastructure.sqs_stitch_queue import STITCH_MESSAGE_VERSION
from src.contexts.library.interface import local_stitch_worker
from src.contexts.library.interface.local_stitch_worker import poll_once

QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/000000000000/bookloud-local-stitch"


class StubUseCase:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.executed: list[str] = []

    def execute(self, command):
        if self.error is not None:
            raise self.error
        self.executed.append(command.book_id)
        return StitchBookResult("PARTIAL", reason="NO_AUDIO")


class FakeSqsClient:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages
        self.deleted_receipt_handles: list[str] = []
        self.receive_calls: list[dict] = []

    def receive_message(self, *, QueueUrl: str, MaxNumberOfMessages: int, WaitTimeSeconds: int, AttributeNames=None):
        self.receive_calls.append({"AttributeNames": AttributeNames})
        return {"Messages": self._messages} if self._messages else {}

    def delete_message(self, *, QueueUrl: str, ReceiptHandle: str) -> None:
        self.deleted_receipt_handles.append(ReceiptHandle)


def _message(book_id: str, receipt_handle: str, *, receive_count: str = "1") -> dict:
    body = json.dumps({"v": STITCH_MESSAGE_VERSION, "userId": "user-1", "bookId": book_id})
    return {
        "Body": body,
        "ReceiptHandle": receipt_handle,
        "MessageId": f"msg-{receipt_handle}",
        "Attributes": {"ApproximateReceiveCount": receive_count},
    }


def test_poll_once_no_messages_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_stitch_worker, "_build_use_case", lambda: StubUseCase())
    sqs = FakeSqsClient([])
    assert poll_once(sqs, QUEUE_URL) == 0
    assert sqs.deleted_receipt_handles == []


def test_poll_once_requests_approximate_receive_count_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_stitch_worker, "_build_use_case", lambda: StubUseCase())
    sqs = FakeSqsClient([])
    poll_once(sqs, QUEUE_URL)
    assert sqs.receive_calls[0]["AttributeNames"] == ["ApproximateReceiveCount"]


def test_poll_once_processes_and_deletes_a_successful_message(monkeypatch: pytest.MonkeyPatch) -> None:
    use_case = StubUseCase()
    monkeypatch.setattr(local_stitch_worker, "_build_use_case", lambda: use_case)
    sqs = FakeSqsClient([_message("book-1", "rh-1")])

    assert poll_once(sqs, QUEUE_URL) == 1
    assert sqs.deleted_receipt_handles == ["rh-1"]
    assert use_case.executed == ["book-1"]


def test_poll_once_forwards_approximate_receive_count_into_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this, local dev would never exercise StitchBook's last-attempt
    rule (the PARTIAL/STITCH_FAILED degradation)."""
    captured: list[int] = []

    class CapturingUseCase:
        def execute(self, command):
            captured.append(command.attempt)
            return StitchBookResult("PARTIAL")

    monkeypatch.setattr(local_stitch_worker, "_build_use_case", lambda: CapturingUseCase())
    poll_once(FakeSqsClient([_message("book-1", "rh-1", receive_count="3")]), QUEUE_URL)

    assert captured == [3]


def test_poll_once_leaves_a_failed_message_undeleted_for_redelivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        local_stitch_worker, "_build_use_case", lambda: StubUseCase(error=RuntimeError("S3 is down"))
    )
    sqs = FakeSqsClient([_message("book-1", "rh-1")])

    assert poll_once(sqs, QUEUE_URL) == 0
    assert sqs.deleted_receipt_handles == []


def test_poll_once_processes_each_message_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[StubUseCase] = []

    def build():
        use_case = StubUseCase(error=RuntimeError("boom") if len(calls) == 1 else None)
        calls.append(use_case)
        return use_case

    monkeypatch.setattr(local_stitch_worker, "_build_use_case", build)
    sqs = FakeSqsClient([_message("book-1", "rh-1"), _message("book-2", "rh-2")])

    assert poll_once(sqs, QUEUE_URL) == 1
    assert sqs.deleted_receipt_handles == ["rh-1"]
    assert len(calls) == 2


def test_build_use_case_returns_a_real_stitch_book(dynamodb_table) -> None:
    """Exercises the real (non-monkeypatched) composition root."""
    from src.contexts.library.application.stitching import StitchBook

    assert isinstance(local_stitch_worker._build_use_case(), StitchBook)
