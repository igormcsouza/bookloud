from __future__ import annotations

import pytest

from src.contexts.library.domain.extraction import ExtractionError
from src.contexts.library.domain.value_objects import ExtractionFailure
from src.contexts.library.infrastructure.pymupdf_extractor import PyMuPdfTextExtractor
from tests.contexts.library.pdf_fixtures import (
    CORRUPT_PDF_BYTES,
    empty_pdf,
    encrypted_pdf,
    footnote_pdf,
    headered_pdf,
    hyphenated_pdf,
    image_only_pdf,
    long_paragraph_pdf,
    multipage_pdf,
    owner_password_pdf,
    simple_text_pdf,
)


@pytest.fixture
def extractor() -> PyMuPdfTextExtractor:
    return PyMuPdfTextExtractor()


# --- happy path / offsets ----------------------------------------------------


def test_simple_text_pdf_extracts_body_text(extractor: PyMuPdfTextExtractor) -> None:
    document = extractor.extract(simple_text_pdf())
    assert "Hello, this is a simple test document." in document.text
    assert document.page_count == 1
    assert len(document.pages) == 1
    assert document.pages[0].number == 1
    assert document.pages[0].char_start == 0
    assert document.pages[0].char_end == len(document.text)


def test_extract_returns_zero_dropped_counts_for_plain_document(
    extractor: PyMuPdfTextExtractor,
) -> None:
    document = extractor.extract(simple_text_pdf())
    assert document.dropped_running_lines == 0
    assert document.dropped_footnote_blocks == 0


# --- multi-page mapping -------------------------------------------------------


def test_multipage_pdf_produces_one_page_per_physical_page(
    extractor: PyMuPdfTextExtractor,
) -> None:
    document = extractor.extract(multipage_pdf(pages=5))
    assert document.page_count == 5
    assert len(document.pages) == 5
    assert [p.number for p in document.pages] == [1, 2, 3, 4, 5]


def test_multipage_pdf_page_ranges_are_contiguous_and_cover_the_text(
    extractor: PyMuPdfTextExtractor,
) -> None:
    document = extractor.extract(multipage_pdf(pages=5))
    assert document.pages[0].char_start == 0
    for page in document.pages:
        assert f"page {page.number}" in document.text[page.char_start : page.char_end]
    assert document.pages[-1].char_end == len(document.text)


# --- header/footer edge case, end to end --------------------------------------


def test_headered_pdf_drops_running_header_and_footer_page_number(
    extractor: PyMuPdfTextExtractor,
) -> None:
    document = extractor.extract(headered_pdf(pages=6))
    assert "Bookloud User Guide" not in document.text
    assert "Page 1" not in document.text
    assert "Page 6" not in document.text
    assert document.dropped_running_lines > 0


def test_headered_pdf_keeps_body_text(extractor: PyMuPdfTextExtractor) -> None:
    document = extractor.extract(headered_pdf(pages=6))
    for i in range(1, 7):
        assert f"Chapter content for page {i}" in document.text


# --- footnote edge case, end to end --------------------------------------------


def test_footnote_pdf_drops_footnote_block_and_superscript_marker(
    extractor: PyMuPdfTextExtractor,
) -> None:
    document = extractor.extract(footnote_pdf())
    assert document.dropped_footnote_blocks == 1
    assert "A footnote explaining the claim" not in document.text
    # The superscript marker digits are stripped from the inline sentence.
    assert "This sentence makes a claim" in document.text
    assert "that needs support" in document.text


# --- de-hyphenation ------------------------------------------------------------


def test_hyphenated_pdf_dehyphenates_across_line_break(extractor: PyMuPdfTextExtractor) -> None:
    document = extractor.extract(hyphenated_pdf())
    assert "example of hyphenation" in document.text
    assert "exam-\nple" not in document.text
    assert "exam- ple" not in document.text


# --- long paragraph / sentence-split path --------------------------------------


def test_long_paragraph_pdf_produces_a_single_paragraph(
    extractor: PyMuPdfTextExtractor,
) -> None:
    document = extractor.extract(long_paragraph_pdf(chars=8000))
    # A true single paragraph has no blank-line separator anywhere -- this is
    # what forces domain/chunking.py's sentence-split fallback rather than
    # the paragraph-greedy path.
    assert "\n\n" not in document.text
    assert len(document.text) > 2600  # comfortably past chunk_text's `maximum`


# --- encryption ------------------------------------------------------------


def test_encrypted_pdf_raises_encrypted_pdf(extractor: PyMuPdfTextExtractor) -> None:
    with pytest.raises(ExtractionError) as excinfo:
        extractor.extract(encrypted_pdf())
    assert excinfo.value.reason == ExtractionFailure.ENCRYPTED_PDF


def test_owner_password_only_pdf_extracts_successfully(
    extractor: PyMuPdfTextExtractor,
) -> None:
    """Regression guard (PLANS/phase-3.md §10.1): an owner-password-only PDF
    must NOT be treated as encrypted -- ``needs_pass`` is already false for
    it, and it must extract normally."""
    document = extractor.extract(owner_password_pdf())
    assert "Owner-protected but readable contents" in document.text


# --- other permanent failures ---------------------------------------------


def test_image_only_pdf_raises_no_text_layer(extractor: PyMuPdfTextExtractor) -> None:
    with pytest.raises(ExtractionError) as excinfo:
        extractor.extract(image_only_pdf())
    assert excinfo.value.reason == ExtractionFailure.NO_TEXT_LAYER


def test_empty_pdf_raises_empty_pdf(extractor: PyMuPdfTextExtractor) -> None:
    with pytest.raises(ExtractionError) as excinfo:
        extractor.extract(empty_pdf())
    assert excinfo.value.reason == ExtractionFailure.EMPTY_PDF


def test_corrupt_pdf_raises_corrupt_pdf(extractor: PyMuPdfTextExtractor) -> None:
    with pytest.raises(ExtractionError) as excinfo:
        extractor.extract(CORRUPT_PDF_BYTES)
    assert excinfo.value.reason == ExtractionFailure.CORRUPT_PDF


def test_oversized_bytes_raise_too_large_before_opening() -> None:
    extractor = PyMuPdfTextExtractor(max_bytes=10)
    with pytest.raises(ExtractionError) as excinfo:
        extractor.extract(b"0123456789ABCDEF")
    assert excinfo.value.reason == ExtractionFailure.TOO_LARGE


def test_default_max_bytes_matches_the_shared_upload_cap() -> None:
    from src.contexts.library.domain.storage import MAX_UPLOAD_BYTES

    extractor = PyMuPdfTextExtractor()
    assert extractor._max_bytes == MAX_UPLOAD_BYTES  # noqa: SLF001


def test_unexpected_error_during_processing_raises_unknown(
    monkeypatch: pytest.MonkeyPatch, extractor: PyMuPdfTextExtractor
) -> None:
    """Anything that goes wrong *after* the PDF opens successfully (not a
    parse/encryption/empty-page condition already handled) must still land
    as a permanent ``ExtractionError(UNKNOWN, ...)`` -- PLANS/phase-3.md
    §8.3's UNKNOWN row -- rather than propagate as a bare exception."""
    import src.contexts.library.infrastructure.pymupdf_extractor as module

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("unexpected pymupdf failure")

    monkeypatch.setattr(module, "find_running_lines", boom)

    with pytest.raises(ExtractionError) as excinfo:
        extractor.extract(simple_text_pdf())
    assert excinfo.value.reason == ExtractionFailure.UNKNOWN
