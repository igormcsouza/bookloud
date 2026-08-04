"""Ports shared across bounded contexts (as opposed to a single context's own
``application/ports.py``). Empty implementations land alongside the contexts
that need them, starting in phase 2."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...  # pragma: no cover


class IdGenerator(Protocol):
    def new_id(self) -> str: ...  # pragma: no cover
