"""``SynthesisQueue`` adapter over SQS ``SendMessageBatch`` (PLANS/
phase-4.md §4.4) -- the fan-out producer the extract Lambda calls after the
``EXTRACTED`` flip.

Message body: ``{"v": 1, "userId": "<sub>", "bookId": "<uuid>", "chunkIndex": 7}``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from src.infrastructure.aws import client

SYNTHESIS_MESSAGE_VERSION = 1
_BATCH_SIZE = 10  # SendMessageBatch hard cap (also 256 KB total; entries
# here are ~110 bytes, so batching is purely about call count)


class SqsSynthesisQueue:
    def __init__(self, *, queue_url: str, sqs: Any | None = None) -> None:
        self._queue_url = queue_url
        self._injected_sqs = sqs

    @property
    def _sqs(self) -> Any:
        """Built lazily, on first send, through src.infrastructure.aws.client
        ("sqs") so LocalStack works with zero code change.

        Lazy for the same reason ``DynamoDbBookRepository`` resolves
        ``library_table()`` at call time (see ``interface/dependencies.py``):
        constructing this adapter is pure DI wiring and must not touch the
        environment. Unlike S3, SQS has no global endpoint, so building the
        client eagerly makes ``get_synthesis_queue()`` raise ``NoRegionError``
        anywhere ``AWS_REGION`` is unset -- which is exactly the credential-free
        environment the backend test job runs in.
        """
        if self._injected_sqs is None:
            self._injected_sqs = client("sqs")
        return self._injected_sqs

    def enqueue_chunks(self, *, user_id: str, book_id: str, chunk_indexes: Iterable[int]) -> int:
        indexes = list(chunk_indexes)
        sent = 0
        for start in range(0, len(indexes), _BATCH_SIZE):
            batch = indexes[start : start + _BATCH_SIZE]
            entries = [self._entry(user_id=user_id, book_id=book_id, index=index) for index in batch]
            sent += self._send_batch_with_retry(entries)
        return sent

    def _entry(self, *, user_id: str, book_id: str, index: int) -> dict:
        return {
            # Id must be unique per SendMessageBatch call and
            # ASCII-alphanumeric -- str(index) satisfies both since each
            # batch only ever contains distinct chunk indexes.
            "Id": str(index),
            "MessageBody": json.dumps(
                {"v": SYNTHESIS_MESSAGE_VERSION, "userId": user_id, "bookId": book_id, "chunkIndex": index}
            ),
        }

    def _send_batch_with_retry(self, entries: list[dict]) -> int:
        response = self._sqs.send_message_batch(QueueUrl=self._queue_url, Entries=entries)
        successful = len(response.get("Successful", []))
        failed = response.get("Failed", [])
        if not failed:
            return successful

        # send_message_batch partially succeeds -- silently dropping a
        # failed entry means one chunk is never synthesized and the book
        # never completes (a genuine correctness bug, not a nicety).
        # Retry the failed entries exactly once.
        failed_ids = {entry["Id"] for entry in failed}
        retry_entries = [entry for entry in entries if entry["Id"] in failed_ids]
        retry_response = self._sqs.send_message_batch(QueueUrl=self._queue_url, Entries=retry_entries)
        successful += len(retry_response.get("Successful", []))
        still_failed = retry_response.get("Failed", [])
        if still_failed:
            raise RuntimeError(f"Failed to enqueue chunk messages after retry: {still_failed}")
        return successful
