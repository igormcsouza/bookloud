from __future__ import annotations

import json
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from src.contexts.library.infrastructure.sqs_synthesis_queue import (
    SYNTHESIS_MESSAGE_VERSION,
    SqsSynthesisQueue,
)

QUEUE_NAME = "bookloud-test-synthesize"


@pytest.fixture
def sqs_queue(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("sqs", region_name="us-east-1")
        queue_url = client.create_queue(QueueName=QUEUE_NAME)["QueueUrl"]
        yield client, queue_url


def _all_messages(client, queue_url: str) -> list[dict]:
    messages: list[dict] = []
    while True:
        response = client.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0)
        batch = response.get("Messages", [])
        if not batch:
            break
        messages.extend(batch)
        client.delete_message_batch(
            QueueUrl=queue_url, Entries=[{"Id": m["MessageId"], "ReceiptHandle": m["ReceiptHandle"]} for m in batch]
        )
    return messages


# --- batching ----------------------------------------------------------------


def test_enqueue_chunks_sends_one_message_per_index(sqs_queue) -> None:
    client, queue_url = sqs_queue
    queue = SqsSynthesisQueue(queue_url=queue_url, sqs=client)

    sent = queue.enqueue_chunks(user_id="user-1", book_id="book-1", chunk_indexes=range(5))

    assert sent == 5
    messages = _all_messages(client, queue_url)
    assert len(messages) == 5


def test_message_body_shape(sqs_queue) -> None:
    client, queue_url = sqs_queue
    queue = SqsSynthesisQueue(queue_url=queue_url, sqs=client)

    queue.enqueue_chunks(user_id="user-1", book_id="book-1", chunk_indexes=[7])

    (message,) = _all_messages(client, queue_url)
    body = json.loads(message["Body"])
    assert body == {"v": SYNTHESIS_MESSAGE_VERSION, "userId": "user-1", "bookId": "book-1", "chunkIndex": 7}


def test_25_chunk_book_produces_3_batch_calls() -> None:
    stub_sqs = MagicMock()
    stub_sqs.send_message_batch.return_value = {
        "Successful": [{"Id": str(i)} for i in range(10)],
        "Failed": [],
    }
    queue = SqsSynthesisQueue(queue_url="https://sqs.example/q", sqs=stub_sqs)

    sent = queue.enqueue_chunks(user_id="u", book_id="b", chunk_indexes=range(25))

    # 25 chunks / 10-per-batch cap -> ceil(25/10) == 3 calls.
    assert stub_sqs.send_message_batch.call_count == 3
    assert sent == 30  # each call's mocked "Successful" always returns 10 regardless of batch size


def test_batch_entries_have_unique_ascii_alphanumeric_ids() -> None:
    stub_sqs = MagicMock()
    stub_sqs.send_message_batch.return_value = {"Successful": [], "Failed": []}
    queue = SqsSynthesisQueue(queue_url="https://sqs.example/q", sqs=stub_sqs)

    queue.enqueue_chunks(user_id="u", book_id="b", chunk_indexes=range(10))

    (call,) = stub_sqs.send_message_batch.call_args_list
    entries = call.kwargs["Entries"]
    ids = [e["Id"] for e in entries]
    assert len(ids) == len(set(ids))
    assert all(i.isalnum() and i.isascii() for i in ids)


def test_empty_chunk_indexes_sends_nothing() -> None:
    stub_sqs = MagicMock()
    queue = SqsSynthesisQueue(queue_url="https://sqs.example/q", sqs=stub_sqs)

    sent = queue.enqueue_chunks(user_id="u", book_id="b", chunk_indexes=[])

    assert sent == 0
    stub_sqs.send_message_batch.assert_not_called()


# --- partial failure retry ---------------------------------------------------


def test_partial_failure_is_retried_once_and_succeeds() -> None:
    stub_sqs = MagicMock()
    stub_sqs.send_message_batch.side_effect = [
        {"Successful": [{"Id": "0"}, {"Id": "1"}], "Failed": [{"Id": "2", "Message": "throttled"}]},
        {"Successful": [{"Id": "2"}], "Failed": []},
    ]
    queue = SqsSynthesisQueue(queue_url="https://sqs.example/q", sqs=stub_sqs)

    sent = queue.enqueue_chunks(user_id="u", book_id="b", chunk_indexes=[0, 1, 2])

    assert sent == 3
    assert stub_sqs.send_message_batch.call_count == 2
    retry_call = stub_sqs.send_message_batch.call_args_list[1]
    retry_ids = [e["Id"] for e in retry_call.kwargs["Entries"]]
    assert retry_ids == ["2"]


def test_partial_failure_still_failing_after_retry_raises_runtime_error() -> None:
    stub_sqs = MagicMock()
    stub_sqs.send_message_batch.side_effect = [
        {"Successful": [{"Id": "0"}], "Failed": [{"Id": "1", "Message": "throttled"}]},
        {"Successful": [], "Failed": [{"Id": "1", "Message": "throttled again"}]},
    ]
    queue = SqsSynthesisQueue(queue_url="https://sqs.example/q", sqs=stub_sqs)

    with pytest.raises(RuntimeError):
        queue.enqueue_chunks(user_id="u", book_id="b", chunk_indexes=[0, 1])

    assert stub_sqs.send_message_batch.call_count == 2
