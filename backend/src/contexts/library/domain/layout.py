"""Pure header/footer/footnote policy over plain-dataclass text geometry.
**No pymupdf, no boto3 import** -- everything here operates on
``TextSpan``/``TextLine``/``TextBlock``/``PageLayout``, so the header/footer
and footnote edge cases (PLANS/phase-3.md §7.3, the plan's highest-priority
correctness area) are exhaustively unit-testable without a real PDF.
``infrastructure/pymupdf_extractor.py`` is the only module that builds these
dataclasses from an actual document and calls into this one.

**Deviation from the plan's bare function signatures** (flagged per the
task's instructions to note-and-proceed on genuine ambiguity): §7.3(b)/(c)
give ``is_footnote_block(block, *, body_size, page_height) -> bool`` and
``strip_superscript_markers(line) -> str``, but the prose right below each
requires information those signatures can't carry -- "not the only block on
the page" needs the page's block count, and the superscript ratio needs
``body_size``. Both gained one extra required keyword-only parameter
(``page_block_count`` / ``body_size`` respectively) so the described
algorithm is actually implementable and independently testable; every other
signature in this module matches the plan verbatim.

Non-goal, stated explicitly per §7.3: multi-column layouts. PyMuPDF's
``sort=True`` interleaves text across true side-by-side columns, and nothing
here corrects for it -- out of scope for phase 3 (plan §13 OQ-6).
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

HEADER_BAND_FRACTION = 0.08
FOOTER_BAND_FRACTION = 0.92
FOOTNOTE_BAND_FRACTION = 0.75
RUNNING_LINE_REPETITION_THRESHOLD = 0.5
RUNNING_LINE_MIN_PAGES = 3
FOOTNOTE_SIZE_RATIO = 0.85
SUPERSCRIPT_SIZE_RATIO = 0.75


@dataclass(frozen=True)
class TextSpan:
    text: str
    size: float
    origin_y: float


@dataclass(frozen=True)
class TextLine:
    spans: tuple[TextSpan, ...]
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def text(self) -> str:
        return "".join(span.text for span in self.spans)

    @property
    def size(self) -> float:
        return _modal_size(self.spans)


@dataclass(frozen=True)
class TextBlock:
    lines: tuple[TextLine, ...]
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class PageLayout:
    number: int
    width: float
    height: float
    blocks: tuple[TextBlock, ...]


# --- (a) running headers/footers ----------------------------------------

_DIGIT_RUN_RE = re.compile(r"\d+")
_EDGE_NON_WORD_RE = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)
_ROMAN_NUMERAL_RE = re.compile(r"^[ivxlcdm]+$", re.IGNORECASE)
_BARE_PAGE_NUMBER_RE = re.compile(r"^[^\w]*\d+[^\w]*$", re.UNICODE)


def normalize_running(text: str) -> str:
    """Casefold + NFKC, collapse whitespace, strip leading/trailing
    punctuation, then replace every run of digits with ``#`` -- so "Page 5
    of 200" and "Page 6 of 200" normalize identically and are recognized as
    the same running line across pages despite the varying page number."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = " ".join(normalized.split())
    normalized = _EDGE_NON_WORD_RE.sub("", normalized)
    normalized = _DIGIT_RUN_RE.sub("#", normalized)
    return normalized


def is_bare_page_number(text: str) -> bool:
    """A band line containing nothing but a (optionally decorated, e.g.
    "- 12 -") Arabic page number or a roman numeral -- dropped
    unconditionally, regardless of repetition, so a 1-2 page document (too
    short for the repetition threshold to ever fire) still loses its bare
    page number."""
    stripped = text.strip()
    if not stripped:
        return False
    if _BARE_PAGE_NUMBER_RE.match(stripped):
        return True
    return bool(_ROMAN_NUMERAL_RE.match(stripped))


def find_running_lines(pages: Sequence[PageLayout]) -> frozenset[str]:
    """Normalized forms of lines that sit entirely inside the header/footer
    band on >= 50% of pages -- only evaluated once ``page_count >= 3``
    (below that, the unconditional ``is_bare_page_number`` rule is the only
    defence; a 1-2 page repetition signal is too noisy to trust)."""
    page_count = len(pages)
    if page_count < RUNNING_LINE_MIN_PAGES:
        return frozenset()

    counts: Counter[str] = Counter()
    for page in pages:
        seen_this_page: set[str] = set()
        for block in page.blocks:
            for line in block.lines:
                if not _in_band(line, page.height):
                    continue
                normalized = normalize_running(line.text)
                if normalized:
                    seen_this_page.add(normalized)
        counts.update(seen_this_page)

    threshold = RUNNING_LINE_REPETITION_THRESHOLD * page_count
    return frozenset(norm for norm, n in counts.items() if n >= threshold)


def is_running_or_page_number(
    line: TextLine, page_height: float, running: frozenset[str]
) -> bool:
    """Single call the extractor makes per band-candidate line: drop it if
    it's a bare page number (unconditional) or its normalized form is one of
    the document's running lines. Non-band lines (headings in the body
    region) are never dropped -- that's the point of the band gate."""
    if not _in_band(line, page_height):
        return False
    if is_bare_page_number(line.text):
        return True
    normalized = normalize_running(line.text)
    return bool(normalized) and normalized in running


def _in_band(line: TextLine, page_height: float) -> bool:
    header_limit = HEADER_BAND_FRACTION * page_height
    footer_limit = FOOTER_BAND_FRACTION * page_height
    return line.y1 <= header_limit or line.y0 >= footer_limit


# --- (b) footnote blocks --------------------------------------------------


def modal_span_size(pages: Sequence[PageLayout]) -> float:
    """Char-weighted mode of span sizes across the whole document -- the
    "body text" size the footnote/superscript heuristics compare against.
    Mode, not mean, so a handful of oversized drop-caps or a run of small
    italics don't skew the baseline."""
    all_spans = [
        span
        for page in pages
        for block in page.blocks
        for line in block.lines
        for span in line.spans
    ]
    return _modal_size(all_spans)


def is_footnote_block(
    block: TextBlock,
    *,
    body_size: float,
    page_height: float,
    page_block_count: int,
) -> bool:
    """A block is a footnote when it starts in the bottom 25% of the page,
    its char-weighted median span size is <= 85% of ``body_size``, and it
    is not the only block on the page (``page_block_count`` -- see the
    module docstring's deviation note)."""
    if page_block_count <= 1:
        return False
    if block.y0 < FOOTNOTE_BAND_FRACTION * page_height:
        return False
    spans = [span for line in block.lines for span in line.spans]
    if not spans:
        return False
    median_size = _weighted_median_size(spans)
    return median_size <= FOOTNOTE_SIZE_RATIO * body_size


# --- (c) inline superscript footnote markers ------------------------------

_SUPERSCRIPT_TEXT_RE = re.compile(r"^(\(?\d+\)?|[*†‡§¶]+)$")


def strip_superscript_markers(line: TextLine, *, body_size: float) -> str:
    """Drop spans that look like an inline footnote marker: small
    (<= 75% of ``body_size``), raised above the line's dominant baseline,
    and textually only digits / ``*†‡§¶`` / bracketed digits.
    Returns the line's text with those spans removed -- everything else is
    kept verbatim."""
    if not line.spans:
        return ""
    baseline = _modal_origin_y(line.spans)
    kept: list[str] = []
    for span in line.spans:
        if (
            span.size <= SUPERSCRIPT_SIZE_RATIO * body_size
            # Smaller origin_y = higher on the (top-left-origin) page, i.e.
            # visually "above" the line's dominant baseline.
            and span.origin_y < baseline
            and _SUPERSCRIPT_TEXT_RE.match(span.text.strip())
        ):
            continue
        kept.append(span.text)
    return "".join(kept)


# --- shared weighted-stat helpers -----------------------------------------


def _modal_size(spans: Sequence[TextSpan]) -> float:
    weights: Counter[float] = Counter()
    for span in spans:
        weights[span.size] += len(span.text) or 1
    if not weights:
        return 0.0
    return max(weights.items(), key=lambda kv: (kv[1], -kv[0]))[0]


def _modal_origin_y(spans: Sequence[TextSpan]) -> float:
    weights: Counter[float] = Counter()
    for span in spans:
        weights[span.origin_y] += len(span.text) or 1
    if not weights:
        return 0.0
    return max(weights.items(), key=lambda kv: (kv[1], -kv[0]))[0]


def _weighted_median_size(spans: Sequence[TextSpan]) -> float:
    pairs = sorted((span.size, len(span.text) or 1) for span in spans)
    total = sum(weight for _, weight in pairs)
    if total == 0:
        return 0.0
    half = total / 2
    cumulative = 0
    for size, weight in pairs:
        cumulative += weight
        if cumulative >= half:
            return size
    return pairs[-1][0]
