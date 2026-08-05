"""Ports and value objects for PDF text extraction (PLANS/phase-3.md §7.2).
No pymupdf import here either -- ``PdfTextExtractor`` is a ``Protocol``;
``infrastructure/pymupdf_extractor.py`` is the only module that implements
it and the only module allowed to import ``pymupdf``.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from src.contexts.library.domain.value_objects import ExtractionFailure
from src.shared_kernel.domain.errors import DomainError


@dataclass(frozen=True)
class ExtractedPage:
    number: int  # 1-based
    char_start: int  # into ExtractedDocument.text
    char_end: int  # half-open


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    pages: tuple[ExtractedPage, ...]
    page_count: int
    dropped_running_lines: int
    dropped_footnote_blocks: int


class ExtractionError(DomainError):
    """A permanent extraction failure (PLANS/phase-3.md §8.3) -- caught by
    ``ExtractBook`` and turned into ``Book.status = FAILED`` +
    ``failure_reason``. Distinct from an unhandled exception, which
    ``ExtractBook`` lets propagate so SQS retries (transient errors)."""

    status_code = 422

    def __init__(self, reason: ExtractionFailure, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class PdfTextExtractor(Protocol):
    def extract(self, pdf_bytes: bytes) -> ExtractedDocument: ...  # pragma: no cover


def page_range_for(
    pages: Sequence[ExtractedPage], char_start: int, char_end: int
) -> tuple[int, int]:
    """1-based ``(page_start, page_end)`` inclusive range covering
    ``[char_start, char_end)`` of the document's full extracted text, via
    ``bisect_right`` over each page's ``char_start``. Pages that produced no
    text have a zero-length range and are therefore never selected."""
    if not pages:
        return (0, 0)

    starts = [page.char_start for page in pages]

    # bisect_right(starts, char_start) gives the index of the first page
    # whose char_start is > char_start; the page containing char_start is
    # therefore the one before it.
    start_index = max(0, bisect_right(starts, char_start) - 1)

    # For the end offset, char_end is half-open/exclusive, so the "last
    # character" of the range is at char_end - 1 (guard char_end == char_start,
    # an empty chunk, by clamping to char_start itself).
    last_char = max(char_start, char_end - 1)
    end_index = max(0, bisect_right(starts, last_char) - 1)

    return (pages[start_index].number, pages[end_index].number)
