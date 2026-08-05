"""``PdfTextExtractor`` adapter backed by PyMuPDF (PLANS/phase-3.md §7.5).

This is the **only** module in the codebase allowed to ``import pymupdf`` --
``domain/extraction.py``'s ``PdfTextExtractor`` Protocol and
``domain/layout.py``'s pure geometry types are what let the header/footer/
footnote policy be exhaustively unit-tested without a real PDF; this module
is the thin, geometry-building glue between PyMuPDF's own ``dict`` text
extraction and that pure policy layer.
"""

from __future__ import annotations

import re
import unicodedata

import pymupdf

from src.contexts.library.domain.extraction import ExtractedDocument, ExtractedPage, ExtractionError
from src.contexts.library.domain.layout import (
    PageLayout,
    TextBlock,
    TextLine,
    TextSpan,
    find_running_lines,
    is_footnote_block,
    is_running_or_page_number,
    modal_span_size,
    strip_superscript_markers,
)
from src.contexts.library.domain.storage import MAX_UPLOAD_BYTES
from src.contexts.library.domain.value_objects import ExtractionFailure

# Below this many non-whitespace characters in the whole document, treat the
# PDF as having no usable text layer (the scanned-image case) -- PLANS/
# phase-3.md §7.5 step 10.
MIN_TEXT_CHARS = 100

_TEXT_BLOCK_TYPE = 0  # PyMuPDF's "dict" block type for text (1 == image).
_HYPHENS = ("-", "­")  # ASCII hyphen + soft hyphen
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


class PyMuPdfTextExtractor:
    """Satisfies ``PdfTextExtractor`` structurally (no inheritance)."""

    def __init__(self, *, max_bytes: int = MAX_UPLOAD_BYTES) -> None:
        self._max_bytes = max_bytes

    def extract(self, pdf_bytes: bytes) -> ExtractedDocument:
        if len(pdf_bytes) > self._max_bytes:
            raise ExtractionError(ExtractionFailure.TOO_LARGE, "PDF exceeds the upload size limit")

        try:
            doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        except Exception as exc:
            raise ExtractionError(ExtractionFailure.CORRUPT_PDF, "Could not open PDF") from exc

        try:
            return self._extract_from_document(doc)
        except ExtractionError:
            raise
        except Exception as exc:
            # Anything else (a PyMuPDF internal error mid-parse, etc.) is a
            # permanent failure too -- PLANS/phase-3.md §8.3's ``UNKNOWN``
            # row -- rather than an unhandled exception that would look like
            # a transient error and trigger pointless SQS retries.
            raise ExtractionError(ExtractionFailure.UNKNOWN, "Unexpected error extracting PDF text") from exc
        finally:
            doc.close()

    def _extract_from_document(self, doc: pymupdf.Document) -> ExtractedDocument:
        if doc.needs_pass and not doc.authenticate(""):
            # Owner-password-only PDFs authenticate with "" and never reach
            # here (needs_pass is already false for them) -- see
            # pdf_fixtures.owner_password_pdf's regression-guard test.
            raise ExtractionError(ExtractionFailure.ENCRYPTED_PDF, "PDF is password protected")

        if doc.page_count == 0:
            raise ExtractionError(ExtractionFailure.EMPTY_PDF, "PDF has no pages")

        layouts = [_build_page_layout(doc[i], number=i + 1) for i in range(doc.page_count)]

        running = find_running_lines(layouts)
        body_size = modal_span_size(layouts)

        dropped_running_lines = 0
        dropped_footnote_blocks = 0
        page_texts: list[str] = []
        for layout in layouts:
            page_text, running_dropped, footnote_dropped = _render_page(layout, running, body_size)
            page_texts.append(_normalize_text(page_text))
            dropped_running_lines += running_dropped
            dropped_footnote_blocks += footnote_dropped

        text, pages = _assemble(page_texts)

        if len(text.strip()) < MIN_TEXT_CHARS:
            raise ExtractionError(ExtractionFailure.NO_TEXT_LAYER, "PDF has no usable text layer")

        return ExtractedDocument(
            text=text,
            pages=tuple(pages),
            page_count=doc.page_count,
            dropped_running_lines=dropped_running_lines,
            dropped_footnote_blocks=dropped_footnote_blocks,
        )


def _build_page_layout(page: pymupdf.Page, *, number: int) -> PageLayout:
    raw = page.get_text("dict", sort=True)
    blocks: list[TextBlock] = []
    for raw_block in raw.get("blocks", ()):
        if raw_block.get("type") != _TEXT_BLOCK_TYPE:
            continue  # image/other non-text block
        lines: list[TextLine] = []
        for raw_line in raw_block.get("lines", ()):
            spans = tuple(
                TextSpan(text=s["text"], size=float(s["size"]), origin_y=float(s["origin"][1]))
                for s in raw_line.get("spans", ())
                if s.get("text")
            )
            if not spans:
                continue  # pragma: no cover -- defensive; PyMuPDF doesn't emit empty spans
            x0, y0, x1, y1 = raw_line["bbox"]
            lines.append(TextLine(spans=spans, x0=x0, y0=y0, x1=x1, y1=y1))
        if not lines:
            continue  # pragma: no cover -- defensive; PyMuPDF doesn't emit line-less text blocks
        bx0, by0, bx1, by1 = raw_block["bbox"]
        blocks.append(TextBlock(lines=tuple(lines), x0=bx0, y0=by0, x1=bx1, y1=by1))
    return PageLayout(number=number, width=raw["width"], height=raw["height"], blocks=tuple(blocks))


def _render_page(
    layout: PageLayout, running: frozenset[str], body_size: float
) -> tuple[str, int, int]:
    """Returns ``(page_text, dropped_running_lines, dropped_footnote_blocks)``."""
    dropped_running = 0
    dropped_footnotes = 0
    block_texts: list[str] = []

    for block in layout.blocks:
        if is_footnote_block(
            block,
            body_size=body_size,
            page_height=layout.height,
            page_block_count=len(layout.blocks),
        ):
            dropped_footnotes += 1
            continue

        line_texts: list[str] = []
        for line in block.lines:
            if is_running_or_page_number(line, layout.height, running):
                dropped_running += 1
                continue
            stripped = strip_superscript_markers(line, body_size=body_size)
            if stripped.strip():
                line_texts.append(stripped)

        block_text = _join_lines(line_texts)
        if block_text.strip():
            block_texts.append(block_text)

    return "\n\n".join(block_texts), dropped_running, dropped_footnotes


def _join_lines(lines: list[str]) -> str:
    """Join a text block's lines into one paragraph string, de-hyphenating
    a trailing ``-``/soft-hyphen line break (PLANS/phase-3.md §7.5 step 7)
    rather than inserting a space or a line break there."""
    result = ""
    for raw_line in lines:
        line = raw_line.rstrip()
        if not line:
            continue  # pragma: no cover -- defensive; callers only pass non-blank lines
        if not result:
            result = line
        elif result.endswith(_HYPHENS):
            result = result[:-1] + line
        else:
            result = f"{result} {line}"
    return result


def _normalize_text(text: str) -> str:
    """NFKC, CRLF -> LF, NBSP -> space, trailing-space-per-line stripped,
    3+ blank lines collapsed to one blank line -- applied per page, before
    offsets are computed (PLANS/phase-3.md §7.5 step 8), so the offsets
    stored on ``ExtractedPage``/chunks never desync from the normalized
    text."""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace(" ", " ")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    normalized = _MULTI_NEWLINE_RE.sub("\n\n", normalized)
    return normalized.strip()


def _assemble(page_texts: list[str]) -> tuple[str, list[ExtractedPage]]:
    """Concatenate per-page text with ``"\\n\\n"``, recording each page's
    ``[char_start, char_end)`` range -- a page with no text gets a
    zero-length range (never selected by ``page_range_for``)."""
    pieces: list[str] = []
    pages: list[ExtractedPage] = []
    pos = 0
    for i, page_text in enumerate(page_texts):
        if i > 0:
            pieces.append("\n\n")
            pos += 2
        start = pos
        pieces.append(page_text)
        pos += len(page_text)
        pages.append(ExtractedPage(number=i + 1, char_start=start, char_end=pos))
    return "".join(pieces), pages
