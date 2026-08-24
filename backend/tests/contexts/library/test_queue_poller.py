from __future__ import annotations

from src.contexts.library.interface.queue_poller import drain_queue


class _FakeContext:
    """Stand-in for the Lambda context's `get_remaining_time_in_millis`,
    counting down by a fixed step on every call so tests can pin exactly how
    many polls happen before the safety margin is hit."""

    def __init__(self, start_ms: int, step_ms: int) -> None:
        self._remaining = start_ms
        self._step = step_ms

    def get_remaining_time_in_millis(self) -> int:
        value = self._remaining
        self._remaining -= self._step
        return value


def test_drain_queue_stops_when_poll_once_returns_zero() -> None:
    calls = []

    def poll_once(sqs, queue_url):
        calls.append(queue_url)
        return 0

    total = drain_queue(poll_once, sqs=object(), queue_url="q", context=_FakeContext(60_000, 1_000))

    assert total == 0
    assert len(calls) == 1


def test_drain_queue_keeps_polling_while_messages_are_found() -> None:
    remaining_batches = [3, 2, 0]

    def poll_once(sqs, queue_url):
        return remaining_batches.pop(0)

    total = drain_queue(poll_once, sqs=object(), queue_url="q", context=_FakeContext(60_000, 1_000))

    assert total == 5
    assert remaining_batches == []


def test_drain_queue_stops_at_the_safety_margin_even_with_backlog_remaining() -> None:
    """A queue that never empties (pathological backlog) must not run
    forever -- the loop yields back to the scheduler once remaining time
    drops to the safety margin, leaving the rest for the next tick."""
    calls = 0

    def poll_once(sqs, queue_url):
        nonlocal calls
        calls += 1
        return 1  # always "found a message" -- infinite backlog

    context = _FakeContext(start_ms=35_000, step_ms=10_000)
    total = drain_queue(
        poll_once, sqs=object(), queue_url="q", context=context, safety_margin_ms=30_000
    )

    # remaining_time reads: 35000 (>30000, poll), 25000 (<=30000, stop).
    assert calls == 1
    assert total == 1


def test_drain_queue_never_polls_if_already_under_the_safety_margin() -> None:
    def poll_once(sqs, queue_url):
        raise AssertionError("should never be called")

    context = _FakeContext(start_ms=10_000, step_ms=1_000)
    total = drain_queue(
        poll_once, sqs=object(), queue_url="q", context=context, safety_margin_ms=30_000
    )

    assert total == 0


def test_drain_queue_passes_through_the_queue_url() -> None:
    seen_urls = []

    def poll_once(sqs, queue_url):
        seen_urls.append(queue_url)
        return 0

    drain_queue(
        poll_once, sqs="sqs-client", queue_url="https://example/q", context=_FakeContext(60_000, 1_000)
    )

    assert seen_urls == ["https://example/q"]
