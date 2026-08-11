"""``StitchQueue`` adapter over SQS ``SendMessage`` (PLANS/phase-5.md §4.4)
-- the fan-in producer the synthesize Lambda calls when its
``increment_chunks_done`` is the one that observes ``chunks_done ==
chunks_total``.

Message body: ``{"v": 1, "userId": "<sub>", "bookId": "<uuid>"}``. One message
per book, so no batching and no partial-failure handling (unlike
``sqs_synthesis_queue.py``'s fan-out).
"""

from __future__ import annotations

import json
from typing import Any

from src.infrastructure.aws import client

STITCH_MESSAGE_VERSION = 1


class SqsStitchQueue:
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
        client eagerly makes ``get_stitch_queue()`` raise ``NoRegionError``
        anywhere ``AWS_REGION`` is unset -- which is exactly the
        credential-free environment the backend test job runs in. This is not
        hypothetical: an eager client in the phase-4 sibling adapter broke CI.
        """
        if self._injected_sqs is None:
            self._injected_sqs = client("sqs")
        return self._injected_sqs

    def enqueue_book(self, *, user_id: str, book_id: str) -> None:
        self._sqs.send_message(
            QueueUrl=self._queue_url,
            MessageBody=json.dumps(
                {"v": STITCH_MESSAGE_VERSION, "userId": user_id, "bookId": book_id}
            ),
        )
