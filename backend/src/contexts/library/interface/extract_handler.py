"""Extract Lambda handlers -- the pipeline's "controller" (PLANS/phase-3.md
§8.1). Wired in ``infra/stacks/pipeline_stack.py`` as a second
``DockerImageFunction`` over the same backend image as the API Lambda, with
``cmd=["src.contexts.library.interface.extract_handler.scheduled_handler"]``.

``scheduled_handler`` is the deployed entrypoint: an EventBridge Rule
invokes it on a fixed schedule instead of an SQS event source mapping
invoking ``handler`` continuously (that ESM used to poll the queue 24/7,
even idle -- the SQS free-tier cost trap ``pipeline_stack.py``'s
``_add_scheduled_pollers`` explains). It drains the queue with the same
``poll_once`` the ``extract-worker`` docker-compose service already runs
against LocalStack (``local_extract_worker.py``).

``handler`` (the plain ``event["Records"]`` shape) is kept for direct/test
invocation -- nothing in AWS calls it anymore.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from collections.abc import Sequence

from src.contexts.library.application.extraction import ExtractBook, ExtractBookCommand, ExtractBookResult
from src.contexts.library.infrastructure.s3_keys import parse_source_pdf_key
from src.contexts.library.interface.dependencies import (
    get_book_repository,
    get_chunk_repository,
    get_clock,
    get_pdf_extractor,
    get_pdf_storage,
    get_synthesis_queue,
)

logger = logging.getLogger("bookloud.extract")

_OBJECT_CREATED_PREFIX = "ObjectCreated"


def handler(event: dict, context: object) -> None:
    # Composition root built per invocation (never at import) -- the same
    # reasoning as interface/dependencies.py's FastAPI providers: this is
    # what lets mock_aws()/monkeypatch (tests) and LocalStack both work.
    use_case = ExtractBook(
        book_repository=get_book_repository(),
        chunk_repository=get_chunk_repository(),
        pdf_storage=get_pdf_storage(),
        extractor=get_pdf_extractor(),
        clock=get_clock(),
        synthesis_queue=get_synthesis_queue(),
    )
    handle_records(event.get("Records", []), use_case)


def scheduled_handler(event: dict, context: object) -> None:
    """EventBridge Rule entrypoint (``infra/stacks/pipeline_stack.py``'s
    ``_add_scheduled_pollers``). Ignores ``event`` -- the rule's schedule is
    the only trigger that matters -- and drains the extract queue with the
    same ``poll_once`` the local docker-compose worker uses."""
    from src.config import settings
    from src.contexts.library.interface.local_extract_worker import poll_once
    from src.contexts.library.interface.queue_poller import drain_queue
    from src.infrastructure.aws import client

    drain_queue(poll_once, client("sqs"), settings.extract_queue_url, context)


def handle_records(records: Sequence[dict], use_case: ExtractBook) -> list[ExtractBookResult]:
    results: list[ExtractBookResult] = []
    for record in records:
        body = record.get("body", "")
        for bucket, key, size in s3_objects_from_sqs_body(body):
            parsed = parse_source_pdf_key(key)
            if parsed is None:
                logger.warning("Ignoring S3 event for unrecognized key: bucket=%s key=%s", bucket, key)
                continue
            user_id, book_id = parsed
            command = ExtractBookCommand(user_id=user_id, book_id=book_id, source_key=key, size_bytes=size)
            result = use_case.execute(command)
            logger.info(
                "ExtractBook %s/%s -> %s%s",
                user_id,
                book_id,
                result.outcome,
                f" ({result.reason})" if result.reason else "",
            )
            results.append(result)
    return results


def s3_objects_from_sqs_body(body: str) -> list[tuple[str, str, int]]:
    """Parse one SQS message body (an S3 event notification envelope) into
    ``(bucket, key, size)`` tuples. Two gotchas handled explicitly:

    - ``s3:TestEvent`` -- posted once when the bucket notification is first
      configured -- has no ``Records`` and is ignored.
    - S3 always URL-encodes the key in event notifications; always
      ``unquote_plus`` it back.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return []

    if payload.get("Event") == "s3:TestEvent":
        return []

    objects: list[tuple[str, str, int]] = []
    for record in payload.get("Records", []):
        event_name = record.get("eventName", "")
        if not event_name.startswith(_OBJECT_CREATED_PREFIX):
            continue
        s3_info = record.get("s3", {})
        bucket = s3_info.get("bucket", {}).get("name", "")
        obj = s3_info.get("object", {})
        raw_key = obj.get("key", "")
        if not bucket or not raw_key:
            continue
        key = urllib.parse.unquote_plus(raw_key)
        size = int(obj.get("size", 0) or 0)
        objects.append((bucket, key, size))
    return objects
