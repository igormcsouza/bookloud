"""Book-level stitching: the offset math and the book manifest -- **PURE**
(PLANS/phase-5.md §7.2/§7.3). No ``boto3``, no network, no MP3 parsing.

Same reasoning as ``domain/marks.py`` and ``infrastructure/mp3.py``: the
book-global timeline is exactly what phase 6's highlight sync depends on, and
it is exactly the part no automated check can ever observe end to end (§3.1 --
real TTS is prod-only, so no environment CI can reach ever produces audio to
stitch). So it has to be exhaustively unit-testable on its own.

``StitchQueue`` lives here for the same reason ``SynthesisQueue`` lives in
``domain/synthesis.py``: ``SynthesizeChunk`` publishes the fan-in trigger
through a port, not through boto3.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.value_objects import BookStatus

MANIFEST_SCHEMA_VERSION = 1


class StitchQueue(Protocol):
    """The fan-in trigger port -- ``SynthesizeChunk`` depends on this, not on
    boto3/SQS. Mirrors ``domain/synthesis.py``'s ``SynthesisQueue``."""

    def enqueue_book(self, *, user_id: str, book_id: str) -> None: ...  # pragma: no cover


@dataclass(frozen=True)
class SegmentInput:
    """One ``DONE`` chunk's contribution to the book timeline.

    ``duration_seconds`` is a **float** on purpose (see ``plan_segments``).
    ``byte_len`` is ``None`` on the degraded ``STITCH_FAILED`` path, where
    the durations come from the stored ``Chunk.duration_ms`` and no
    concatenated file exists to have byte offsets into.
    """

    index: int
    duration_seconds: float
    char_start: int
    char_end: int
    audio_key: str
    marks_key: str | None = None
    byte_len: int | None = None


@dataclass(frozen=True)
class StitchSegment:
    index: int
    start_ms: int
    duration_ms: int
    char_start: int
    char_end: int
    audio_key: str
    marks_key: str | None
    byte_start: int | None = None
    byte_end: int | None = None


def plan_segments(entries: Sequence[SegmentInput]) -> tuple[list[StitchSegment], int]:
    """Rebase per-chunk durations into a book-global timeline.

    Accumulates in FLOAT SECONDS and rounds ONCE per boundary::

        cum += duration_seconds
        t_next = round(cum * 1000);  d = t_next - t;  t = t_next

    so segment boundaries exactly partition the timeline (no gap, no overlap)
    and ``sum(d) == total_ms`` exactly. Summing per-segment *rounded* integers
    instead would accumulate up to 0.5 ms of error per segment -- ~165 ms over
    a 330-chunk book, i.e. a visible highlight lag by the end of a long book,
    growing monotonically (PLANS/phase-5.md §7.3/Q8).

    Byte offsets accumulate the same way and are half-open (``b0`` inclusive,
    ``b1`` exclusive), so ``segments[k].byte_start == segments[k-1].byte_end``.
    They are impossible to recover later without rescanning every frame, so
    they are computed here, for free, while the bytes go past.

    Returns ``(segments, total_ms)``.
    """
    segments: list[StitchSegment] = []
    cumulative_seconds = 0.0
    start_ms = 0
    byte_cursor = 0

    for entry in entries:
        cumulative_seconds += entry.duration_seconds
        next_ms = round(cumulative_seconds * 1000)
        if entry.byte_len is None:
            byte_start: int | None = None
            byte_end: int | None = None
        else:
            byte_start = byte_cursor
            byte_end = byte_cursor + entry.byte_len
            byte_cursor = byte_end
        segments.append(
            StitchSegment(
                index=entry.index,
                start_ms=start_ms,
                duration_ms=next_ms - start_ms,
                char_start=entry.char_start,
                char_end=entry.char_end,
                audio_key=entry.audio_key,
                marks_key=entry.marks_key,
                byte_start=byte_start,
                byte_end=byte_end,
            )
        )
        start_ms = next_ms

    return segments, start_ms


def build_book_manifest(
    *,
    book: Book,
    status: BookStatus,
    segments: Sequence[StitchSegment],
    missing: Sequence[int],
    book_audio_key: str | None,
    total_ms: int,
    sample_rate_hz: int | None,
) -> dict:
    """Build ``marks/<userId>/<bookId>/book.json`` (PLANS/phase-5.md §7.2's
    shape). Phase 6 and phase 7 both consume this, so it is a contract.

    Asserts ``segments`` is ascending in ``i`` and that ``t``/``d`` partition
    ``[0, total_ms)`` contiguously -- the manifest *is* the timeline, and a
    single out-of-order or overlapping entry silently corrupts phase 6's
    lookup (which is a ``bisect_right`` over ``t``).

    ``status`` is passed explicitly rather than read off ``book``: the
    manifest is written *before* the terminal DynamoDB transition (audio ->
    manifest -> DynamoDB), so at build time ``book.status`` is still
    ``STITCHING`` while the document must already advertise its final
    ``READY``/``PARTIAL``.
    """
    previous_index = -1
    expected_start = 0
    for segment in segments:
        assert segment.index > previous_index, "segments must be ascending by chunk index"
        previous_index = segment.index
        assert segment.start_ms == expected_start, "segment timeline must be contiguous"
        expected_start = segment.start_ms + segment.duration_ms
    assert expected_start == total_ms, "sum of segment durations must equal total_ms"

    return {
        "version": MANIFEST_SCHEMA_VERSION,
        "bookId": book.id,
        "status": status.value,
        "audioKey": book_audio_key,
        "durationMs": total_ms,
        "chunksTotal": book.chunks_total,
        "chunksDone": book.chunks_done,
        "chunksFailed": book.chunks_failed,
        "sampleRateHz": sample_rate_hz,
        # Short keys for the per-segment hot fields, long keys for the
        # once-per-document ones -- the same trade-off phase-4 §7.5 made:
        # a 330-segment document stays ~60 KB and still readable in the S3
        # console while debugging a desync.
        "segments": [_segment_to_dict(segment) for segment in segments],
        "missing": sorted(missing),
    }


def _segment_to_dict(segment: StitchSegment) -> dict:
    entry: dict = {
        "i": segment.index,
        "t": segment.start_ms,
        "d": segment.duration_ms,
        # Book-global CHARACTER offsets -- deliberately asymmetric with the
        # per-chunk marks files, whose s/e are chunk-relative (phase-4 §7.5):
        # the chunk file indexes into one chunk's text, the manifest indexes
        # into the book. Both are documented in README.md.
        "s": segment.char_start,
        "e": segment.char_end,
        # Always present, even when the document-level audioKey is null --
        # THE invariant that makes graceful degradation a data-model
        # property: a book whose concatenation failed still plays and
        # highlights chunk by chunk (PLANS/phase-5.md §1 invariant 3).
        "audioKey": segment.audio_key,
        "marksKey": segment.marks_key,
    }
    if segment.byte_start is not None and segment.byte_end is not None:
        entry["b0"] = segment.byte_start
        entry["b1"] = segment.byte_end
    return entry
