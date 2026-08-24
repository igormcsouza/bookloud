"""Time-boxed drain loop shared by every pipeline Lambda's ``scheduled_handler``
(``infra/stacks/pipeline_stack.py``'s EventBridge Rule targets, replacing the
SQS event source mappings that used to poll each queue 24/7 -- see that
stack's ``_add_scheduled_pollers`` docstring for the free-tier cost reasoning).

Each queue's own ``poll_once`` (``local_extract_worker.py``/
``local_synthesize_worker.py``/``local_stitch_worker.py`` -- already exercised
by the local-dev poll loop against LocalStack) does the actual receive/
process/delete-on-success work. ``drain_queue`` just calls it repeatedly
until the queue reports empty or the invocation is close to timing out, so
one scheduled tick clears out whatever backlog built up since the last one
instead of processing a single message and exiting.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

PollOnce = Callable[[Any, str], int]

# Left as headroom for the in-flight poll_once call (up to a 20s long-poll
# wait plus one message's processing) to finish before the Lambda's own
# timeout cuts it off mid-delete -- a killed-mid-delete message would be
# reprocessed anyway (SQS visibility timeout), but finishing cleanly avoids
# burning a redelivery attempt for no reason.
DEFAULT_SAFETY_MARGIN_MS = 30_000


def drain_queue(
    poll_once: PollOnce,
    sqs: Any,
    queue_url: str,
    context: Any,
    *,
    safety_margin_ms: int = DEFAULT_SAFETY_MARGIN_MS,
) -> int:
    """Calls ``poll_once(sqs, queue_url)`` until it returns 0 (queue empty)
    or ``context.get_remaining_time_in_millis()`` drops to ``safety_margin_ms``
    or below. Returns the total number of messages processed."""
    total = 0
    while context.get_remaining_time_in_millis() > safety_margin_ms:
        processed = poll_once(sqs, queue_url)
        total += processed
        if processed == 0:
            break
    return total
