"""Pure, exhaustive coverage of the book-global offset math and the manifest
schema (PLANS/phase-5.md §10.2). No AWS, no I/O, no MP3 parsing -- this is
the part phase 6's highlight sync depends on and the part no automated check
in any reachable environment will ever observe end to end.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.stitching import (
    MANIFEST_SCHEMA_VERSION,
    SegmentInput,
    StitchSegment,
    build_book_manifest,
    plan_segments,
)
from src.contexts.library.domain.value_objects import BookStatus

FIXED_NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


def _entry(index: int, seconds: float, *, byte_len: int | None = 1000) -> SegmentInput:
    return SegmentInput(
        index=index,
        duration_seconds=seconds,
        char_start=index * 100,
        char_end=(index + 1) * 100,
        audio_key=f"audio/u/b/{index:06d}.mp3",
        marks_key=f"marks/u/b/{index:06d}.json",
        byte_len=byte_len,
    )


def _book(*, chunks_total: int = 3, chunks_done: int = 3, chunks_failed: int = 0) -> Book:
    book = Book.create(id="book-1", user_id="user-1", title_raw="A Book", now=FIXED_NOW)
    book.chunks_total = chunks_total
    book.chunks_done = chunks_done
    book.chunks_failed = chunks_failed
    return book


# --- plan_segments ----------------------------------------------------------


def test_empty_input_returns_no_segments_and_zero_total() -> None:
    assert plan_segments([]) == ([], 0)


def test_single_segment_starts_at_zero() -> None:
    segments, total = plan_segments([_entry(0, 1.234)])
    (segment,) = segments
    assert segment.start_ms == 0
    assert segment.duration_ms == 1234
    assert total == 1234


def test_segments_are_contiguous_and_sum_exactly() -> None:
    segments, total = plan_segments([_entry(0, 1.1), _entry(1, 2.2), _entry(2, 3.3)])

    assert [s.start_ms for s in segments] == [0, 1100, 3300]
    for previous, current in zip(segments, segments[1:]):
        assert current.start_ms == previous.start_ms + previous.duration_ms
    assert sum(s.duration_ms for s in segments) == total


def test_rounding_does_not_drift_over_330_segments() -> None:
    """THE regression test for §7.3/Q8. Each segment is 0.0245 s -- exactly
    24.5 ms, i.e. the pathological half-millisecond case. Accumulating in
    float seconds and rounding once per boundary keeps sum(d) == total_ms
    exactly; rounding each segment first would drift by ~165 ms."""
    count = 330
    entries = [_entry(i, 0.0245) for i in range(count)]

    segments, total = plan_segments(entries)

    assert sum(s.duration_ms for s in segments) == total
    assert total == round(count * 0.0245 * 1000)
    naive = sum(round(0.0245 * 1000) for _ in range(count))
    # The naive per-segment rounding is off by ~165 ms -- proving the test
    # would actually catch a regression to that implementation.
    assert abs(naive - total) >= 100


def test_a_gap_in_chunk_indexes_leaves_the_timeline_contiguous() -> None:
    """Failed chunks are simply absent (§7.2/Q10) -- the *audio* has no hole,
    so t must stay contiguous even though i jumps."""
    segments, total = plan_segments([_entry(0, 1.0), _entry(7, 2.0), _entry(42, 0.5)])

    assert [s.index for s in segments] == [0, 7, 42]
    assert [s.start_ms for s in segments] == [0, 1000, 3000]
    assert total == 3500


def test_byte_ranges_are_half_open_and_contiguous() -> None:
    segments, _ = plan_segments(
        [_entry(0, 1.0, byte_len=100), _entry(1, 1.0, byte_len=250), _entry(2, 1.0, byte_len=50)]
    )
    assert [(s.byte_start, s.byte_end) for s in segments] == [(0, 100), (100, 350), (350, 400)]


def test_missing_byte_len_leaves_byte_range_absent() -> None:
    """The degraded STITCH_FAILED path: durations come from the stored
    Chunk.duration_ms and there is no concatenated file to index into."""
    segments, total = plan_segments([_entry(0, 1.0, byte_len=None), _entry(1, 2.0, byte_len=None)])
    assert all(s.byte_start is None and s.byte_end is None for s in segments)
    assert total == 3000


# --- build_book_manifest ----------------------------------------------------


def test_manifest_has_the_full_documented_schema() -> None:
    segments, total = plan_segments([_entry(0, 1.0), _entry(1, 2.0)])

    document = build_book_manifest(
        book=_book(chunks_total=3, chunks_done=3, chunks_failed=1),
        status=BookStatus.PARTIAL,
        segments=segments,
        missing=[2],
        book_audio_key="audio/user-1/book-1/book.mp3",
        total_ms=total,
        sample_rate_hz=24000,
    )

    assert document == {
        "version": MANIFEST_SCHEMA_VERSION,
        "bookId": "book-1",
        "status": "PARTIAL",
        "audioKey": "audio/user-1/book-1/book.mp3",
        "durationMs": 3000,
        "chunksTotal": 3,
        "chunksDone": 3,
        "chunksFailed": 1,
        "sampleRateHz": 24000,
        "segments": [
            {
                "i": 0, "t": 0, "d": 1000, "s": 0, "e": 100,
                "audioKey": "audio/u/b/000000.mp3", "marksKey": "marks/u/b/000000.json",
                "b0": 0, "b1": 1000,
            },
            {
                "i": 1, "t": 1000, "d": 2000, "s": 100, "e": 200,
                "audioKey": "audio/u/b/000001.mp3", "marksKey": "marks/u/b/000001.json",
                "b0": 1000, "b1": 2000,
            },
        ],
        "missing": [2],
    }


def test_manifest_sorts_missing_ascending() -> None:
    document = build_book_manifest(
        book=_book(), status=BookStatus.PARTIAL, segments=[], missing=[42, 7, 1],
        book_audio_key=None, total_ms=0, sample_rate_hz=None,
    )
    assert document["missing"] == [1, 7, 42]


def test_zero_segment_manifest_is_still_a_valid_document() -> None:
    """The steady state of local dev and every PR stack (§3.1): no audio at
    all, but a real book.json is still written."""
    document = build_book_manifest(
        book=_book(chunks_total=2, chunks_done=2, chunks_failed=2),
        status=BookStatus.PARTIAL, segments=[], missing=[0, 1],
        book_audio_key=None, total_ms=0, sample_rate_hz=None,
    )
    assert document["audioKey"] is None
    assert document["sampleRateHz"] is None
    assert document["segments"] == []
    assert document["durationMs"] == 0


def test_segments_keep_their_own_audio_key_when_the_book_audio_key_is_null() -> None:
    """§1 invariant 3: the manifest is fully usable with a null document-level
    audioKey, so a book whose concatenation failed still plays chunk by
    chunk."""
    segments, total = plan_segments([_entry(0, 1.0, byte_len=None)])
    document = build_book_manifest(
        book=_book(), status=BookStatus.PARTIAL, segments=segments, missing=[],
        book_audio_key=None, total_ms=total, sample_rate_hz=None,
    )
    (entry,) = document["segments"]
    assert entry["audioKey"] == "audio/u/b/000000.mp3"
    assert entry["marksKey"] == "marks/u/b/000000.json"
    assert "b0" not in entry and "b1" not in entry


def test_manifest_rejects_out_of_order_segments() -> None:
    bad = [
        StitchSegment(index=1, start_ms=0, duration_ms=10, char_start=0, char_end=1,
                      audio_key="a", marks_key="m"),
        StitchSegment(index=0, start_ms=10, duration_ms=10, char_start=0, char_end=1,
                      audio_key="a", marks_key="m"),
    ]
    with pytest.raises(AssertionError, match="ascending"):
        build_book_manifest(
            book=_book(), status=BookStatus.READY, segments=bad, missing=[],
            book_audio_key=None, total_ms=20, sample_rate_hz=None,
        )


def test_manifest_rejects_a_gap_in_the_timeline() -> None:
    bad = [
        StitchSegment(index=0, start_ms=0, duration_ms=10, char_start=0, char_end=1,
                      audio_key="a", marks_key="m"),
        StitchSegment(index=1, start_ms=25, duration_ms=10, char_start=0, char_end=1,
                      audio_key="a", marks_key="m"),
    ]
    with pytest.raises(AssertionError, match="contiguous"):
        build_book_manifest(
            book=_book(), status=BookStatus.READY, segments=bad, missing=[],
            book_audio_key=None, total_ms=35, sample_rate_hz=None,
        )


def test_manifest_rejects_a_total_that_disagrees_with_the_segments() -> None:
    segments, _ = plan_segments([_entry(0, 1.0)])
    with pytest.raises(AssertionError, match="total_ms"):
        build_book_manifest(
            book=_book(), status=BookStatus.READY, segments=segments, missing=[],
            book_audio_key=None, total_ms=9999, sample_rate_hz=None,
        )
