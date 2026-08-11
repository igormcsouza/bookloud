from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from src.contexts.library.infrastructure.sqs_stitch_queue import (
    STITCH_MESSAGE_VERSION,
    SqsStitchQueue,
)

QUEUE_NAME = "bookloud-test-stitch"


@pytest.fixture
def sqs_queue(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("sqs", region_name="us-east-1")
        queue_url = client.create_queue(QueueName=QUEUE_NAME)["QueueUrl"]
        yield client, queue_url


def test_enqueue_book_sends_one_message_with_the_documented_body(sqs_queue) -> None:
    client, queue_url = sqs_queue
    queue = SqsStitchQueue(queue_url=queue_url, sqs=client)

    queue.enqueue_book(user_id="user-1", book_id="book-1")

    messages = client.receive_message(
        QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0
    )["Messages"]
    assert len(messages) == 1
    assert json.loads(messages[0]["Body"]) == {
        "v": STITCH_MESSAGE_VERSION,
        "userId": "user-1",
        "bookId": "book-1",
    }


def test_duplicate_publishes_send_two_messages(sqs_queue) -> None:
    """Duplicates are safe by construction (the stitcher's conditional claim),
    so the adapter deliberately does no deduplication of its own."""
    client, queue_url = sqs_queue
    queue = SqsStitchQueue(queue_url=queue_url, sqs=client)

    queue.enqueue_book(user_id="u", book_id="b")
    queue.enqueue_book(user_id="u", book_id="b")

    messages = client.receive_message(
        QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0
    )["Messages"]
    assert len(messages) == 2


def test_construction_without_a_region_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE regression test for the `_sqs` lazy property (PLANS/phase-5.md
    §4.4). Unlike S3, SQS has no global endpoint, so an eagerly constructed
    client raises NoRegionError anywhere AWS_REGION is unset -- which is
    exactly the credential-free, region-free environment the backend CI job
    runs in. This is the exact shape that broke CI in phase 4."""
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)

    queue = SqsStitchQueue(queue_url="https://sqs.example/q")

    assert queue._queue_url == "https://sqs.example/q"  # noqa: SLF001
