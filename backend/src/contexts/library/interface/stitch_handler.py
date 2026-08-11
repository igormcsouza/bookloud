"""SQS-triggered stitch Lambda handler -- the pipeline's "controller"
(PLANS/phase-5.md §6.3). Wired in ``infra/stacks/pipeline_stack.py`` as a
fourth ``DockerImageFunction`` over the same backend image as the API/extract/
synthesize Lambdas, with
``cmd=["src.contexts.library.interface.stitch_handler.handler"]``.

``local_stitch_worker.py`` runs this same ``handle_records`` in a poll loop
against LocalStack, mirroring ``local_synthesize_worker.py``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from src.config import settings
from src.contexts.library.application.stitching import (
    StitchBook,
    StitchBookCommand,
    StitchBookResult,
)
from src.contexts.library.infrastructure.sqs_stitch_queue import STITCH_MESSAGE_VERSION
from src.contexts.library.interface.dependencies import (
    get_audio_storage,
    get_book_repository,
    get_chunk_repository,
    get_clock,
    get_marks_storage,
)

logger = logging.getLogger("bookloud.stitch")


def handler(event: dict, context: object) -> None:
    # Composition root built per invocation (never at import) -- same
    # reasoning as extract_handler.py/synthesize_handler.py: this is what
    # lets mock_aws()/monkeypatch (tests) and LocalStack both work.
    use_case = StitchBook(
        book_repository=get_book_repository(),
        chunk_repository=get_chunk_repository(),
        audio_storage=get_audio_storage(),
        marks_storage=get_marks_storage(),
        clock=get_clock(),
        max_attempts=settings.stitch_max_receive_count,
    )
    handle_records(event.get("Records", []), use_case)


def handle_records(records: Sequence[dict], use_case: StitchBook) -> list[StitchBookResult]:
    results: list[StitchBookResult] = []
    for record in records:
        command = command_from_record(record)
        if command is None:
            continue
        result = use_case.execute(command)
        logger.info(
            "StitchBook %s/%s -> %s%s (%d segment(s), %d missing, %dms)",
            command.user_id,
            command.book_id,
            result.outcome,
            f" ({result.reason})" if result.reason else "",
            result.segments,
            result.missing,
            result.duration_ms,
        )
        results.append(result)
    return results


def command_from_record(record: dict) -> StitchBookCommand | None:
    """Parses one SQS message body (``sqs_stitch_queue.py``'s envelope) into
    a command. Anything unparseable or with an unrecognized ``v`` is ignored
    (logged at WARNING, message deleted -- a poison message must not loop
    forever)."""
    body = record.get("body", "")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Ignoring unparseable stitch message body: %r", body)
        return None

    if not isinstance(payload, dict) or payload.get("v") != STITCH_MESSAGE_VERSION:
        logger.warning("Ignoring stitch message with unrecognized version: %r", payload)
        return None

    try:
        user_id = payload["userId"]
        book_id = payload["bookId"]
    except (KeyError, TypeError):
        logger.warning("Ignoring malformed stitch message: %r", payload)
        return None

    # SQS delivers this as a string; the ESM always populates it. Input to
    # StitchBook's last-attempt rule (PLANS/phase-5.md §3.2's STITCH_FAILED
    # row).
    attempt = int(record.get("attributes", {}).get("ApproximateReceiveCount", "1"))
    return StitchBookCommand(user_id=user_id, book_id=book_id, attempt=attempt)
