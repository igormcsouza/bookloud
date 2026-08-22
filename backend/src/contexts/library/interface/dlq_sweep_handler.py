"""SQS-triggered DLQ-sweeper Lambda handler (``IMPLEMENTATION_PLAN.md`` phase
8; deferred from ``PLANS/phase-3.md`` OQ-4, extended by ``PLANS/phase-5.md``
§4.3/OQ-4). Wired in ``infra/stacks/pipeline_stack.py`` as a fifth
``DockerImageFunction`` over the same backend image as the API/extract/
synthesize/stitch Lambdas, with
``cmd=["src.contexts.library.interface.dlq_sweep_handler.handler"]`` -- fed by
**two** SQS event sources, ``extract_queue``'s DLQ and ``synthesize_queue``'s
DLQ (never ``stitch_queue``'s -- see ``application/sweeping.py``'s module
docstring for why).

A DLQ message's body is byte-for-byte the original message that failed --
this handler distinguishes the two sources by ``eventSourceARN`` (which SQS
queue delivered the batch) and re-uses each pipeline stage's own message
parser (``extract_handler.s3_objects_from_sqs_body``/``parse_source_pdf_key``
for the extract DLQ's S3-event envelope, ``synthesize_handler.command_from_record``
for the synthesize DLQ's ``{"v", "userId", "bookId", "chunkIndex"}`` envelope)
rather than inventing a third parser for content that is already parsed
elsewhere.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from src.contexts.library.application.sweeping import (
    SweepDlq,
    SweepDlqResult,
    SweepExtractDlqCommand,
    SweepSynthesizeDlqCommand,
)
from src.contexts.library.infrastructure.s3_keys import parse_source_pdf_key
from src.contexts.library.interface.dependencies import (
    get_book_repository,
    get_chunk_repository,
    get_stitch_queue,
)
from src.contexts.library.interface.extract_handler import s3_objects_from_sqs_body
from src.contexts.library.interface.synthesize_handler import command_from_record as synthesize_command_from_record

logger = logging.getLogger("bookloud.dlq_sweep")

# Substrings of the event source queue's ARN (``bookloud-<env>-extract-dlq``/
# ``bookloud-<env>-synthesize-dlq``, per pipeline_stack.py's queue_name/
# ``_queue_with_dlq`` naming) -- this is how one Lambda fed by two event
# sources tells which DLQ produced a given batch.
_EXTRACT_DLQ_MARKER = "-extract-dlq"
_SYNTHESIZE_DLQ_MARKER = "-synthesize-dlq"


def handler(event: dict, context: object) -> None:
    # Composition root built per invocation (never at import) -- same
    # reasoning as every other handler in this package: this is what lets
    # mock_aws()/monkeypatch (tests) and LocalStack both work.
    use_case = SweepDlq(
        book_repository=get_book_repository(),
        chunk_repository=get_chunk_repository(),
        stitch_queue=get_stitch_queue(),
    )
    handle_records(event.get("Records", []), use_case)


def handle_records(records: Sequence[dict], use_case: SweepDlq) -> list[SweepDlqResult]:
    results: list[SweepDlqResult] = []
    for record in records:
        source_arn = record.get("eventSourceARN", "")
        if _EXTRACT_DLQ_MARKER in source_arn:
            results.extend(_sweep_extract_record(record, use_case))
        elif _SYNTHESIZE_DLQ_MARKER in source_arn:
            result = _sweep_synthesize_record(record, use_case)
            if result is not None:
                results.append(result)
        else:
            logger.warning("Ignoring DLQ record with unrecognized eventSourceARN: %r", source_arn)
    return results


def _sweep_extract_record(record: dict, use_case: SweepDlq) -> list[SweepDlqResult]:
    results: list[SweepDlqResult] = []
    body = record.get("body", "")
    for _bucket, key, _size in s3_objects_from_sqs_body(body):
        parsed = parse_source_pdf_key(key)
        if parsed is None:
            logger.warning("Ignoring extract DLQ record for unrecognized key: %s", key)
            continue
        user_id, book_id = parsed
        command = SweepExtractDlqCommand(user_id=user_id, book_id=book_id)
        result = use_case.sweep_extract(command)
        logger.info(
            "SweepDlq[extract] %s/%s -> %s%s",
            user_id,
            book_id,
            result.outcome,
            f" ({result.reason})" if result.reason else "",
        )
        results.append(result)
    return results


def _sweep_synthesize_record(record: dict, use_case: SweepDlq) -> SweepDlqResult | None:
    parsed = synthesize_command_from_record(record)
    if parsed is None:
        return None
    command = SweepSynthesizeDlqCommand(
        user_id=parsed.user_id, book_id=parsed.book_id, chunk_index=parsed.chunk_index
    )
    result = use_case.sweep_synthesize(command)
    logger.info(
        "SweepDlq[synthesize] %s/%s#%d -> %s%s",
        command.user_id,
        command.book_id,
        command.chunk_index,
        result.outcome,
        f" ({result.reason})" if result.reason else "",
    )
    return result
