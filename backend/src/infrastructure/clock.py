"""``Clock`` implementation used outside of tests. Fixed/stub clocks live in
``tests/contexts/library/conftest.py`` — production code only ever imports
``SystemClock``."""

from __future__ import annotations

from datetime import UTC, datetime

from src.shared_kernel.application.ports import Clock


class SystemClock:
    """Satisfies ``Clock`` structurally (no inheritance). Always UTC and
    timezone-aware, matching ``IMPLEMENTATION_PLAN.md``'s ISO-8601 UTC
    timestamp convention."""

    def now(self) -> datetime:
        return datetime.now(UTC)
