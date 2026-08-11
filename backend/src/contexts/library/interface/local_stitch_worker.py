"""Local-dev poll loop over the stitch queue, running the **same**
``handle_records`` the stitch Lambda's ``handler`` calls (PLANS/phase-5.md
§9.2). LocalStack community can't run our container-image Lambda, so the
``stitch-worker`` docker-compose service runs this instead: same handler
code, same composition root, polling SQS directly rather than an event source
mapping invoking a Lambda.

``poll_once`` is the testable unit; ``main`` is the infinite loop, excluded
from coverage and exercised only manually (``make up`` / the local-smoke CI
job). Requests ``AttributeNames=["ApproximateReceiveCount"]`` on every
``receive_message`` call and forwards it into the pseudo-record's
``attributes`` -- otherwise local dev would never exercise ``StitchBook``'s
last-attempt rule, a small and easy-to-forget detail.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from src.config import settings
from src.contexts.library.application.stitching import StitchBook
from src.contexts.library.interface.dependencies import (
    get_audio_storage,
    get_book_repository,
    get_chunk_repository,
    get_clock,
    get_marks_storage,
)
from src.contexts.library.interface.stitch_handler import handle_records
from src.infrastructure.aws import client

logger = logging.getLogger("bookloud.stitch_worker")

POLL_WAIT_SECONDS = 10
IDLE_SLEEP_SECONDS = 1
ERROR_SLEEP_SECONDS = 5
MAX_MESSAGES_PER_POLL = 10


def _build_use_case() -> StitchBook:
    # Fresh per message, mirroring the real Lambda's per-invocation
    # composition root (interface/stitch_handler.py's `handler`).
    return StitchBook(
        book_repository=get_book_repository(),
        chunk_repository=get_chunk_repository(),
        audio_storage=get_audio_storage(),
        marks_storage=get_marks_storage(),
        clock=get_clock(),
        max_attempts=settings.stitch_max_receive_count,
    )


def poll_once(sqs: Any, queue_url: str) -> int:
    """Receive up to 10 messages and process each **individually** (not as
    one batch): a message is deleted only if its own ``handle_records`` call
    succeeded. A message that raises (the transient class ``StitchBook``
    re-raises so SQS retries) is left alone so it reappears after the queue's
    visibility timeout, exactly like a real SQS-triggered Lambda invocation
    failing and retrying. Returns the number of messages successfully
    processed."""
    response = sqs.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=MAX_MESSAGES_PER_POLL,
        WaitTimeSeconds=POLL_WAIT_SECONDS,
        AttributeNames=["ApproximateReceiveCount"],
    )
    messages = response.get("Messages", [])
    processed = 0
    for message in messages:
        record = {
            "body": message["Body"],
            "attributes": message.get("Attributes", {}),
        }
        try:
            handle_records([record], _build_use_case())
        except Exception:
            logger.exception(
                "Error processing message %s; leaving for redelivery", message.get("MessageId")
            )
            continue
        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
        processed += 1
    return processed


def main() -> None:  # pragma: no cover -- infinite loop, exercised manually via `make up`
    sqs = client("sqs")
    queue_url = settings.stitch_queue_url
    logger.info("Polling stitch queue: %s", queue_url)
    while True:
        try:
            processed = poll_once(sqs, queue_url)
        except Exception:
            logger.exception("Error while polling the stitch queue")
            time.sleep(ERROR_SLEEP_SECONDS)
            continue
        if processed == 0:
            time.sleep(IDLE_SLEEP_SECONDS)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=settings.log_level)
    main()
