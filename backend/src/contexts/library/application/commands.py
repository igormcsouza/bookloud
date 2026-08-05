"""Application-layer commands for the Library context."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CreateBookCommand:
    user_id: str
    title: str
