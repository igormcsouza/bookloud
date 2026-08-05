from __future__ import annotations

from src.contexts.library.domain.extraction import ExtractedPage, ExtractionError, page_range_for
from src.contexts.library.domain.value_objects import ExtractionFailure


def _pages(*ranges: tuple[int, int]) -> tuple[ExtractedPage, ...]:
    return tuple(
        ExtractedPage(number=i + 1, char_start=start, char_end=end)
        for i, (start, end) in enumerate(ranges)
    )


def test_page_range_for_single_page_range() -> None:
    pages = _pages((0, 100))
    assert page_range_for(pages, 10, 50) == (1, 1)


def test_page_range_for_spans_multiple_pages() -> None:
    pages = _pages((0, 100), (100, 200), (200, 300))
    assert page_range_for(pages, 50, 250) == (1, 3)


def test_page_range_for_exact_page_boundary() -> None:
    pages = _pages((0, 100), (100, 200))
    # A chunk that starts exactly at page 2's char_start belongs to page 2.
    assert page_range_for(pages, 100, 150) == (2, 2)


def test_page_range_for_ends_exactly_at_page_boundary() -> None:
    pages = _pages((0, 100), (100, 200))
    # char_end is half-open/exclusive: [50, 100) belongs entirely to page 1.
    assert page_range_for(pages, 50, 100) == (1, 1)


def test_page_range_for_empty_page_produces_zero_length_range_never_selected() -> None:
    # Page 2 produced no text (char_start == char_end); a chunk spanning
    # pages 1 and 3 should skip straight over it.
    pages = _pages((0, 100), (100, 100), (100, 200))
    assert page_range_for(pages, 50, 150) == (1, 3)


def test_page_range_for_no_pages_returns_zero_zero() -> None:
    assert page_range_for((), 0, 10) == (0, 0)


def test_page_range_for_zero_length_chunk() -> None:
    pages = _pages((0, 100), (100, 200))
    assert page_range_for(pages, 100, 100) == (2, 2)


def test_extraction_error_carries_reason_and_status_code() -> None:
    error = ExtractionError(ExtractionFailure.CORRUPT_PDF, "could not open PDF")
    assert error.reason == ExtractionFailure.CORRUPT_PDF
    assert error.status_code == 422
    assert error.message == "could not open PDF"
