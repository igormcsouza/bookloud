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
    EXTRACTING = "EXTRACTING"  # first SET in phase 3 (extract Lambda claim)
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
    SYNTHESIZING = "SYNTHESIZING"  # first SET in phase 4 (synthesize Lambda claim)
    DONE = "DONE"  # first SET in phase 4
    FAILED = "FAILED"  # first SET in phase 4

    @classmethod
    def parse(cls, value: object) -> ChunkStatus:
        try:
            return cls(value)  # type: ignore[arg-type]
        except ValueError as exc:
            raise ValidationError("Invalid chunk status") from exc


# The exact complement of DONE, so "not yet finished" (i.e. the set of
# statuses from which the synthesize Lambda's claim -> SYNTHESIZING is
# legal) stays a single named concept instead of a negation operator smeared
# across the adapter (PLANS/phase-4.md §5.3). Deliberately includes
# SYNTHESIZING itself: a previous invocation that died hard (OOM, hard
# timeout, Lambda evicted) leaves the chunk in SYNTHESIZING with nothing to
# release it, and there is no claim timestamp to expire -- allowing the
# re-claim costs at worst one duplicated synthesis, and §8.4's exactly-once
# counter still holds.
NON_TERMINAL_CHUNK_STATUSES = (ChunkStatus.PENDING, ChunkStatus.SYNTHESIZING, ChunkStatus.FAILED)


class ExtractionFailure(StrEnum):
    """Permanent extraction failure reasons (PLANS/phase-3.md §5.2/§8.3) --
    stored verbatim as ``Book.failure_reason`` when ``status == FAILED``."""

    CORRUPT_PDF = "CORRUPT_PDF"
    ENCRYPTED_PDF = "ENCRYPTED_PDF"
    EMPTY_PDF = "EMPTY_PDF"
    NO_TEXT_LAYER = "NO_TEXT_LAYER"
    TOO_LARGE = "TOO_LARGE"
    UNKNOWN = "UNKNOWN"


class SynthesisFailure(StrEnum):
    """Permanent chunk-synthesis failure reasons (PLANS/phase-4.md §5.2/
    §8.3/§0) -- stored as ``Chunk.failure_reason`` when ``status ==
    FAILED``."""

    EMPTY_TEXT = "EMPTY_TEXT"
    TEXT_TOO_LONG = "TEXT_TOO_LONG"
    ALL_ENGINES_FAILED = "ALL_ENGINES_FAILED"
    # Raised by StubSynthesizer whenever ENVIRONMENT != "prod" (PLANS/
    # phase-4.md §0) -- every non-prod environment's deterministic terminal
    # state, never a real engine failure.
    EXTERNAL_TTS_DISABLED = "EXTERNAL_TTS_DISABLED"
    UNKNOWN = "UNKNOWN"


class SynthesisSource(StrEnum):
    EDGE_TTS = "edge-tts"
    GOOGLE_TTS = "google-tts"


class MarksTiming(StrEnum):
    MEASURED = "measured"  # per-word events from the engine (edge-tts)
    ESTIMATED = "estimated"  # interpolated between sentence anchors (google)
