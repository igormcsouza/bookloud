from __future__ import annotations

from src.contexts.library.domain.layout import (
    PageLayout,
    TextBlock,
    TextLine,
    TextSpan,
    find_running_lines,
    is_bare_page_number,
    is_footnote_block,
    is_running_or_page_number,
    modal_span_size,
    normalize_running,
    strip_superscript_markers,
)

PAGE_WIDTH = 400.0
PAGE_HEIGHT = 800.0


def _span(text: str, size: float = 11.0, origin_y: float = 0.0) -> TextSpan:
    return TextSpan(text=text, size=size, origin_y=origin_y)


def _line(text: str, y0: float, y1: float, size: float = 11.0, x0: float = 0.0, x1: float = 100.0) -> TextLine:
    return TextLine(spans=(_span(text, size=size, origin_y=y1),), x0=x0, y0=y0, x1=x1, y1=y1)


def _block(lines: tuple[TextLine, ...], y0: float, y1: float, x0: float = 0.0, x1: float = 100.0) -> TextBlock:
    return TextBlock(lines=lines, x0=x0, y0=y0, x1=x1, y1=y1)


def _page(number: int, blocks: tuple[TextBlock, ...], width: float = PAGE_WIDTH, height: float = PAGE_HEIGHT) -> PageLayout:
    return PageLayout(number=number, width=width, height=height, blocks=blocks)


# --- normalize_running -------------------------------------------------------


def test_normalize_running_casefolds_and_collapses_whitespace() -> None:
    assert normalize_running("  Hello   World  ") == "hello world"


def test_normalize_running_replaces_digit_runs_with_hash() -> None:
    assert normalize_running("Page 5 of 200") == "page # of #"


def test_normalize_running_strips_leading_trailing_punctuation() -> None:
    assert normalize_running("- 12 -") == "#"


def test_normalize_running_equivalence_across_varying_page_numbers() -> None:
    assert normalize_running("Chapter 3 -- My Book") == normalize_running("Chapter 9 -- My Book")


# --- is_bare_page_number -----------------------------------------------------


def test_is_bare_page_number_plain_digits() -> None:
    assert is_bare_page_number("12")


def test_is_bare_page_number_decorated() -> None:
    assert is_bare_page_number("- 12 -")
    assert is_bare_page_number("[42]")


def test_is_bare_page_number_roman_numeral() -> None:
    assert is_bare_page_number("iv")
    assert is_bare_page_number("XII")


def test_is_bare_page_number_rejects_real_text() -> None:
    assert not is_bare_page_number("Chapter 3")
    assert not is_bare_page_number("")
    assert not is_bare_page_number("   ")


# --- find_running_lines: repetition threshold -------------------------------


def _headered_page(number: int, total_pages: int, header: str, body: str) -> PageLayout:
    header_line = _line(header, y0=5, y1=10)  # within 8% of 800 = 64
    body_line = _line(body, y0=400, y1=420)
    return _page(number, (_block((header_line,), 5, 10), _block((body_line,), 400, 420)))


def test_find_running_lines_repeated_on_all_pages() -> None:
    pages = [_headered_page(i, 6, "My Book Title", f"Body text page {i}") for i in range(6)]
    running = find_running_lines(pages)
    assert normalize_running("My Book Title") in running


def test_find_running_lines_below_threshold_not_flagged() -> None:
    # Header text is a genuinely distinct word each page (not just a
    # varying digit, which normalize_running would fold to the same "#"
    # form) -- below the 50% threshold for any single normalized form, so
    # nothing should be flagged as running.
    words = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
    pages = [_headered_page(i, 6, f"Unique {words[i]} Header", f"Body text page {i}") for i in range(6)]
    running = find_running_lines(pages)
    assert running == frozenset()


def test_find_running_lines_at_threshold_flagged() -> None:
    # Appears on exactly half (3/6) -- >= 50% threshold.
    headers = ["Repeated"] * 3 + ["Other A", "Other B", "Other C"]
    pages = [_headered_page(i, 6, headers[i], f"body {i}") for i in range(6)]
    running = find_running_lines(pages)
    assert normalize_running("Repeated") in running


def test_find_running_lines_requires_at_least_three_pages() -> None:
    # 2/2 pages repeat the header, but page_count < 3 -- repetition signal
    # is not trusted below that threshold.
    pages = [_headered_page(i, 2, "Same Header", f"body {i}") for i in range(2)]
    running = find_running_lines(pages)
    assert running == frozenset()


def test_find_running_lines_body_region_repeats_not_dropped() -> None:
    # The SAME text repeated on every page, but sitting in the body region
    # (not the header/footer band) -- must never be flagged.
    body_line = _line("Repeated Body Heading", y0=400, y1=420)
    pages = [_page(i, (_block((body_line,), 400, 420),)) for i in range(6)]
    running = find_running_lines(pages)
    assert running == frozenset()


# --- is_running_or_page_number -----------------------------------------------


def test_is_running_or_page_number_true_for_running_line() -> None:
    line = _line("My Book Title", y0=5, y1=10)
    running = frozenset({normalize_running("My Book Title")})
    assert is_running_or_page_number(line, PAGE_HEIGHT, running)


def test_is_running_or_page_number_true_for_bare_page_number_without_repetition() -> None:
    line = _line("42", y0=5, y1=10)
    assert is_running_or_page_number(line, PAGE_HEIGHT, frozenset())


def test_is_running_or_page_number_false_outside_band() -> None:
    line = _line("My Book Title", y0=400, y1=420)  # body region
    running = frozenset({normalize_running("My Book Title")})
    assert not is_running_or_page_number(line, PAGE_HEIGHT, running)


def test_is_running_or_page_number_false_for_unrelated_band_text() -> None:
    line = _line("Some one-off note", y0=5, y1=10)
    assert not is_running_or_page_number(line, PAGE_HEIGHT, frozenset())


# --- modal_span_size: mode, not mean ----------------------------------------


def test_modal_span_size_is_mode_not_mean() -> None:
    # Lots of 11pt body text, one huge 40pt drop-cap -- the mode must be
    # 11, not skewed toward the mean by the outlier.
    body_spans = [_span("word " * 20, size=11.0) for _ in range(10)]
    dropcap = _span("A", size=40.0)
    line = TextLine(spans=(dropcap, *body_spans), x0=0, y0=0, x1=100, y1=20)
    block = _block((line,), 0, 20)
    page = _page(0, (block,))
    assert modal_span_size([page]) == 11.0


def test_modal_span_size_empty_pages() -> None:
    assert modal_span_size([]) == 0.0


# --- is_footnote_block: all branches -----------------------------------------


def test_is_footnote_block_true_when_small_bottom_band_not_only_block() -> None:
    footnote_line = _line("1. A footnote explaining something.", y0=650, y1=660, size=8.0)
    block = _block((footnote_line,), 650, 660)
    assert is_footnote_block(block, body_size=11.0, page_height=PAGE_HEIGHT, page_block_count=2)


def test_is_footnote_block_false_when_only_block_on_page() -> None:
    footnote_line = _line("1. A footnote explaining something.", y0=650, y1=660, size=8.0)
    block = _block((footnote_line,), 650, 660)
    assert not is_footnote_block(block, body_size=11.0, page_height=PAGE_HEIGHT, page_block_count=1)


def test_is_footnote_block_false_when_not_in_bottom_band() -> None:
    line = _line("Regular body text here.", y0=400, y1=420, size=8.0)
    block = _block((line,), 400, 420)
    assert not is_footnote_block(block, body_size=11.0, page_height=PAGE_HEIGHT, page_block_count=2)


def test_is_footnote_block_false_when_size_close_to_body() -> None:
    line = _line("Not actually a footnote, just body text near the bottom.", y0=650, y1=660, size=11.0)
    block = _block((line,), 650, 660)
    assert not is_footnote_block(block, body_size=11.0, page_height=PAGE_HEIGHT, page_block_count=2)


def test_is_footnote_block_false_when_no_spans() -> None:
    empty_line = TextLine(spans=(), x0=0, y0=650, x1=100, y1=660)
    block = _block((empty_line,), 650, 660)
    assert not is_footnote_block(block, body_size=11.0, page_height=PAGE_HEIGHT, page_block_count=2)


def test_is_footnote_block_boundary_size_ratio() -> None:
    # Exactly at the 0.85 threshold -- <= counts as a footnote.
    line = _line("boundary", y0=650, y1=660, size=11.0 * 0.85)
    block = _block((line,), 650, 660)
    assert is_footnote_block(block, body_size=11.0, page_height=PAGE_HEIGHT, page_block_count=2)


# --- strip_superscript_markers: all branches ---------------------------------


def test_strip_superscript_markers_removes_digit_marker() -> None:
    body = _span("word", size=11.0, origin_y=100.0)
    marker = _span("12", size=6.0, origin_y=95.0)  # smaller size, raised (smaller y)
    line = TextLine(spans=(body, marker), x0=0, y0=90, x1=50, y1=100)
    assert strip_superscript_markers(line, body_size=11.0) == "word"


def test_strip_superscript_markers_removes_symbol_marker() -> None:
    body = _span("word", size=11.0, origin_y=100.0)
    marker = _span("*", size=6.0, origin_y=95.0)
    line = TextLine(spans=(body, marker), x0=0, y0=90, x1=50, y1=100)
    assert strip_superscript_markers(line, body_size=11.0) == "word"


def test_strip_superscript_markers_removes_bracketed_digit_marker() -> None:
    body = _span("word", size=11.0, origin_y=100.0)
    marker = _span("(3)", size=6.0, origin_y=95.0)
    line = TextLine(spans=(body, marker), x0=0, y0=90, x1=50, y1=100)
    assert strip_superscript_markers(line, body_size=11.0) == "word"


def test_strip_superscript_markers_keeps_span_when_not_raised() -> None:
    body = _span("word", size=11.0, origin_y=100.0)
    # Same baseline as the body (not raised) -- must be kept even though
    # it's small and numeric.
    not_raised = _span("12", size=6.0, origin_y=100.0)
    line = TextLine(spans=(body, not_raised), x0=0, y0=90, x1=50, y1=100)
    assert strip_superscript_markers(line, body_size=11.0) == "word12"


def test_strip_superscript_markers_keeps_span_when_not_small() -> None:
    body = _span("word", size=11.0, origin_y=100.0)
    # Raised, but not small enough (ratio > 0.75) -- keep it.
    raised_but_large = _span("12", size=10.0, origin_y=95.0)
    line = TextLine(spans=(body, raised_but_large), x0=0, y0=90, x1=50, y1=100)
    assert strip_superscript_markers(line, body_size=11.0) == "word12"


def test_strip_superscript_markers_keeps_span_when_text_not_marker_shaped() -> None:
    body = _span("word", size=11.0, origin_y=100.0)
    # Small and raised, but its text isn't marker-shaped (a real word).
    raised_word = _span("Notes", size=6.0, origin_y=95.0)
    line = TextLine(spans=(body, raised_word), x0=0, y0=90, x1=50, y1=100)
    assert strip_superscript_markers(line, body_size=11.0) == "wordNotes"


def test_strip_superscript_markers_empty_line() -> None:
    line = TextLine(spans=(), x0=0, y0=0, x1=0, y1=0)
    assert strip_superscript_markers(line, body_size=11.0) == ""
