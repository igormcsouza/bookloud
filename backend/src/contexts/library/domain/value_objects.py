"""``BookStatus``/``ChunkStatus`` — the full lifecycle is declared now (see
PLANS/phase-2.md §1.3/§10 Q1), even though Phase 2 code only ever *writes*
``BookStatus.UPLOADED``/``ChunkStatus.PENDING``.

Rationale: the enum is a *parsing* concern before it is a workflow concern.
The mapper must round-trip whatever string is already in the table, so a
deliberately-truncated enum becomes a forward-compat landmine the moment a
later-phase Lambda writes e.g. ``EXTRACTED`` and an older API container (mid
rolling-deploy) tries to read it — that would raise on every ``GET /books``.
Declaring the full set now costs nothing; the *behaviour* that belongs to
later phases (the transition methods, e.g. ``mark_extracted()``) is what this
phase deliberately omits from ``Book``/``Chunk``.
"""

from __future__ import annotations

from enum import StrEnum

from src.shared_kernel.domain.errors import ValidationError


class BookStatus(StrEnum):
    UPLOADED = "UPLOADED"  # set by Book.create() -- phase 2
    EXTRACTED = "EXTRACTED"  # first SET in phase 3 (extract Lambda)
    READY = "READY"  # first SET in phase 5 (stitcher)
    FAILED = "FAILED"  # first SET in phase 3/4 error paths

    @classmethod
    def parse(cls, value: object) -> BookStatus:
        try:
            return cls(value)  # type: ignore[arg-type]
        except ValueError as exc:
            raise ValidationError("Invalid book status") from exc


class ChunkStatus(StrEnum):
    PENDING = "PENDING"  # set by Chunk.create() -- phase 3
    DONE = "DONE"  # first SET in phase 4
    FAILED = "FAILED"  # first SET in phase 4

    @classmethod
    def parse(cls, value: object) -> ChunkStatus:
        try:
            return cls(value)  # type: ignore[arg-type]
        except ValueError as exc:
            raise ValidationError("Invalid chunk status") from exc
