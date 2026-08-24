"""Local-dev poll loop over the synthesize queue, running the **same**
``handle_records`` the synthesize Lambda's ``handler`` calls (PLANS/
phase-4.md §9.2). LocalStack community can't run our container-image
Lambda, so the ``synthesize-worker`` docker-compose service runs this
instead: same handler code, same composition root, polling SQS directly
rather than an event source mapping invoking a Lambda.

``poll_once`` is the testable unit; ``main`` is the infinite loop, excluded
from coverage and exercised only manually (``make up`` / the local-smoke CI
job). Requests ``AttributeNames=["ApproximateReceiveCount"]`` on every
``receive_message`` call and forwards it into the pseudo-record's
``attributes`` -- otherwise local dev would never exercise
``SynthesizeChunk``'s last-attempt rule (§8.3), a small and easy-to-forget
detail.

``synthesize_handler.py``'s ``scheduled_handler`` (the deployed Lambda
entrypoint) also imports ``poll_once`` directly, wrapping it in a
time-boxed drain loop instead of this module's infinite one -- same queue
logic, two different callers.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from src.config import settings
from src.contexts.library.application.synthesis import SynthesizeChunk
from src.contexts.library.interface.dependencies import (
    get_audio_storage,
    get_book_repository,
    get_chunk_repository,
    get_marks_storage,
    get_speech_synthesizer,
    get_stitch_queue,
)
from src.contexts.library.interface.synthesize_handler import handle_records
from src.infrastructure.aws import client

logger = logging.getLogger("bookloud.synthesize_worker")

POLL_WAIT_SECONDS = 10
IDLE_SLEEP_SECONDS = 1
ERROR_SLEEP_SECONDS = 5
MAX_MESSAGES_PER_POLL = 10


def _build_use_case() -> SynthesizeChunk:
    # Fresh per message, mirroring the real Lambda's per-invocation
    # composition root (interface/synthesize_handler.py's `handler`).
    return SynthesizeChunk(
        book_repository=get_book_repository(),
        chunk_repository=get_chunk_repository(),
        synthesizer=get_speech_synthesizer(),
        audio_storage=get_audio_storage(),
        marks_storage=get_marks_storage(),
        stitch_queue=get_stitch_queue(),
        max_attempts=settings.synthesize_max_receive_count,
    )


def poll_once(sqs: Any, queue_url: str) -> int:
    """Receive up to 10 messages and process each **individually** (not as
    one batch): a message is deleted only if its own ``handle_records`` call
    succeeds. A message that raises (a transient error -- the same class
    ``SynthesizeChunk`` re-raises so SQS retries) is left alone so it
    reappears after the queue's visibility timeout, exactly like a real
    SQS-triggered Lambda invocation failing and retrying. Returns the number
    of messages successfully processed."""
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
    queue_url = settings.synthesize_queue_url
    logger.info("Polling synthesize queue: %s", queue_url)
    while True:
        try:
            processed = poll_once(sqs, queue_url)
        except Exception:
            logger.exception("Error while polling the synthesize queue")
            time.sleep(ERROR_SLEEP_SECONDS)
            continue
        if processed == 0:
            time.sleep(IDLE_SLEEP_SECONDS)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=settings.log_level)
    main()
