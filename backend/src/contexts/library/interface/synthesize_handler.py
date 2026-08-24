"""Synthesize Lambda handlers -- the pipeline's "controller" (PLANS/
phase-4.md §8.1). Wired in ``infra/stacks/pipeline_stack.py`` as a third
``DockerImageFunction`` over the same backend image as the API/extract
Lambdas, with
``cmd=["src.contexts.library.interface.synthesize_handler.scheduled_handler"]``.

``scheduled_handler`` is the deployed entrypoint -- an EventBridge Rule
invokes it on a fixed schedule instead of an SQS event source mapping
polling the queue 24/7 (``pipeline_stack.py``'s ``_add_scheduled_pollers``
has the free-tier cost reasoning). It drains the queue with the same
``poll_once`` the ``synthesize-worker`` docker-compose service already runs
against LocalStack (``local_synthesize_worker.py``).

``handler`` (the plain ``event["Records"]`` shape) is kept for direct/test
invocation -- nothing in AWS calls it anymore.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from src.config import settings
from src.contexts.library.application.synthesis import (
    SynthesizeChunk,
    SynthesizeChunkCommand,
    SynthesizeChunkResult,
)
from src.contexts.library.infrastructure.sqs_synthesis_queue import SYNTHESIS_MESSAGE_VERSION
from src.contexts.library.interface.dependencies import (
    get_audio_storage,
    get_book_repository,
    get_chunk_repository,
    get_marks_storage,
    get_speech_synthesizer,
    get_stitch_queue,
)

logger = logging.getLogger("bookloud.synthesize")


def handler(event: dict, context: object) -> None:
    # Composition root built per invocation (never at import) -- same
    # reasoning as extract_handler.py: this is what lets mock_aws()/
    # monkeypatch (tests) and LocalStack both work.
    use_case = SynthesizeChunk(
        book_repository=get_book_repository(),
        chunk_repository=get_chunk_repository(),
        synthesizer=get_speech_synthesizer(),
        audio_storage=get_audio_storage(),
        marks_storage=get_marks_storage(),
        stitch_queue=get_stitch_queue(),
        max_attempts=settings.synthesize_max_receive_count,
    )
    handle_records(event.get("Records", []), use_case)


def scheduled_handler(event: dict, context: object) -> None:
    """EventBridge Rule entrypoint (``infra/stacks/pipeline_stack.py``'s
    ``_add_scheduled_pollers``). Ignores ``event`` -- the rule's schedule is
    the only trigger that matters -- and drains the synthesize queue with the
    same ``poll_once`` the local docker-compose worker uses."""
    from src.config import settings
    from src.contexts.library.interface.local_synthesize_worker import poll_once
    from src.contexts.library.interface.queue_poller import drain_queue
    from src.infrastructure.aws import client

    drain_queue(poll_once, client("sqs"), settings.synthesize_queue_url, context)


def handle_records(records: Sequence[dict], use_case: SynthesizeChunk) -> list[SynthesizeChunkResult]:
    results: list[SynthesizeChunkResult] = []
    for record in records:
        command = command_from_record(record)
        if command is None:
            continue
        result = use_case.execute(command)
        logger.info(
            "SynthesizeChunk %s/%s#%d -> %s%s",
            command.user_id,
            command.book_id,
            command.chunk_index,
            result.outcome,
            f" ({result.reason})" if result.reason else "",
        )
        results.append(result)
    return results


def command_from_record(record: dict) -> SynthesizeChunkCommand | None:
    """Parses one SQS message body (``sqs_synthesis_queue.py``'s envelope)
    into a command. Anything unparseable or with an unrecognized ``v`` is
    ignored (logged at WARNING, message deleted -- a poison message must
    not loop forever)."""
    body = record.get("body", "")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Ignoring unparseable synthesize message body: %r", body)
        return None

    if not isinstance(payload, dict) or payload.get("v") != SYNTHESIS_MESSAGE_VERSION:
        logger.warning("Ignoring synthesize message with unrecognized version: %r", payload)
        return None

    try:
        user_id = payload["userId"]
        book_id = payload["bookId"]
        chunk_index = int(payload["chunkIndex"])
    except (KeyError, TypeError, ValueError):
        logger.warning("Ignoring malformed synthesize message: %r", payload)
        return None

    # SQS delivers this as a string; the ESM always populates it. Input to
    # SynthesizeChunk's last-attempt rule (PLANS/phase-4.md §8.3).
    attempt = int(record.get("attributes", {}).get("ApproximateReceiveCount", "1"))
    return SynthesizeChunkCommand(user_id=user_id, book_id=book_id, chunk_index=chunk_index, attempt=attempt)
