from __future__ import annotations

import pytest

from src.contexts.library.domain.chunking import (
    ChunkBoundary,
    chunk_text,
    split_paragraphs,
    split_sentences,
)

# --- chunk_text: empty / whitespace -----------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\n\n", "\t \n "])
def test_chunk_text_empty_or_whitespace_only_returns_empty_list(text: str) -> None:
    assert chunk_text(text) == []


# --- chunk_text: exact-partition invariant ----------------------------------


def _assert_exact_partition(text: str, chunks: list[ChunkBoundary]) -> None:
    assert chunks, "expected at least one chunk"
    assert chunks[0].char_start == 0
    for a, b in zip(chunks, chunks[1:]):
        assert a.char_end == b.char_start
    assert chunks[-1].char_end == len(text)
    for c in chunks:
        assert text[c.char_start : c.char_end].strip() != ""


def test_chunk_text_exact_partition_simple_text() -> None:
    text = "Hello world. This is a simple paragraph with a few sentences."
    chunks = chunk_text(text)
    _assert_exact_partition(text, chunks)
    # Stored text is the raw slice -- no stripping.
    assert text[chunks[0].char_start : chunks[0].char_end] == text


def test_chunk_text_exact_partition_multi_paragraph() -> None:
    text = "\n\n".join(f"Paragraph {i}. It has a couple of sentences in it." for i in range(20))
    chunks = chunk_text(text)
    _assert_exact_partition(text, chunks)


def test_chunk_text_exact_partition_long_paragraph_sentence_split() -> None:
    sentence = "This is one sentence in a very long paragraph that keeps going. "
    text = sentence * 60  # a single paragraph, well past `maximum`
    chunks = chunk_text(text)
    _assert_exact_partition(text, chunks)
    assert len(chunks) > 1


def test_chunk_text_exact_partition_whitespace_fallback() -> None:
    # A single "sentence" with no terminal punctuation at all, long enough
    # to force the whitespace-split fallback.
    text = "word " * 700  # ~4200 chars, no '.', '!', '?'
    chunks = chunk_text(text, target=1800, maximum=2600, minimum=400)
    _assert_exact_partition(text, chunks)
    assert len(chunks) > 1


def test_chunk_text_unicode_offsets_exact_partition() -> None:
    text = "Héllo wörld. Ünïcödé tëxt with åccénts—and an em dash too. " * 40
    chunks = chunk_text(text)
    _assert_exact_partition(text, chunks)


# --- paragraph boundaries preferred -----------------------------------------


def test_chunk_text_prefers_paragraph_boundaries_when_under_max() -> None:
    p1 = "Short first paragraph."
    p2 = "Short second paragraph."
    text = f"{p1}\n\n{p2}"
    chunks = chunk_text(text, target=10, maximum=1000, minimum=1)
    # Both paragraphs fit comfortably under `maximum`, and `target` is tiny,
    # so each paragraph should close its own chunk rather than merge.
    assert len(chunks) == 2
    assert text[chunks[0].char_start : chunks[0].char_end].strip() == p1
    assert text[chunks[1].char_start : chunks[1].char_end].strip() == p2


def test_chunk_text_merges_small_paragraphs_until_target() -> None:
    paragraphs = [f"Paragraph number {i} is short." for i in range(10)]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, target=100, maximum=1000, minimum=1)
    # Small paragraphs accumulate until `target` is reached at a paragraph
    # boundary -- fewer chunks than paragraphs.
    assert 1 <= len(chunks) < len(paragraphs)


def test_chunk_text_closes_chunk_before_exceeding_maximum() -> None:
    # Two paragraphs whose combined length exceeds `maximum` but each is
    # individually under it -- must not merge past `maximum`.
    p1 = "A" * 60
    p2 = "B" * 60
    text = f"{p1}\n\n{p2}"
    chunks = chunk_text(text, target=200, maximum=100, minimum=1)
    assert len(chunks) == 2
    for c in chunks:
        assert (c.char_end - c.char_start) <= 100 + 2  # +separator slack


# --- sentence-split: never mid-word, abbreviation guard ---------------------


def test_split_sentences_basic() -> None:
    text = "First sentence. Second sentence! Third sentence?"
    spans = split_sentences(text)
    sentences = [text[s:e].strip() for s, e in spans]
    assert sentences == ["First sentence.", "Second sentence!", "Third sentence?"]


def test_split_sentences_abbreviation_guard_does_not_split() -> None:
    text = "Dr. Smith went home. He was tired."
    spans = split_sentences(text)
    sentences = [text[s:e].strip() for s, e in spans]
    assert sentences == ["Dr. Smith went home.", "He was tired."]


@pytest.mark.parametrize(
    "abbr", ["Mr.", "Mrs.", "Ms.", "Dr.", "Prof.", "St.", "Jr.", "Sr.", "vs.", "etc.", "e.g.", "i.e.", "cf.", "Fig.", "No.", "Vol.", "Ch.", "pp."]
)
def test_split_sentences_abbreviation_list_guarded(abbr: str) -> None:
    text = f"See {abbr} example here. Next sentence follows."
    spans = split_sentences(text)
    # The abbreviation's period must not have produced an extra split.
    assert len(spans) == 2


def test_split_sentences_single_capital_initial_guarded() -> None:
    text = "J. K. Rowling wrote the book. It sold well."
    spans = split_sentences(text)
    sentences = [text[s:e].strip() for s, e in spans]
    assert sentences == ["J. K. Rowling wrote the book.", "It sold well."]


def test_chunk_text_sentence_split_never_mid_word() -> None:
    sentence = "This is one sentence in a very long paragraph that keeps repeating itself. "
    text = sentence * 60
    chunks = chunk_text(text, target=500, maximum=800, minimum=100)
    for c in chunks:
        piece = text[c.char_start : c.char_end]
        # No chunk boundary should land inside a word: the char right
        # before char_start (if any) and the char at char_end - 1 combined
        # with what follows should not straddle a word.
        if c.char_start > 0:
            assert not (text[c.char_start - 1].isalnum() and piece[:1].isalnum())
        if c.char_end < len(text):
            assert not (piece[-1:].isalnum() and text[c.char_end].isalnum())


# --- whitespace-split fallback: never mid-word ------------------------------


def test_chunk_text_whitespace_fallback_never_mid_word() -> None:
    text = "word " * 700
    chunks = chunk_text(text, target=1800, maximum=2600, minimum=400)
    for c in chunks:
        piece = text[c.char_start : c.char_end]
        if c.char_start > 0:
            assert not (text[c.char_start - 1].isalnum() and piece[:1].isalnum())
        if c.char_end < len(text):
            assert not (piece[-1:].isalnum() and text[c.char_end].isalnum())


def test_chunk_text_whitespace_fallback_hard_cut_for_unbroken_word() -> None:
    # A single "word" (no whitespace at all) longer than `maximum` has no
    # choice but a hard cut.
    text = "x" * 5000
    chunks = chunk_text(text, target=1800, maximum=2600, minimum=400)
    _assert_exact_partition(text, chunks)
    assert len(chunks) > 1


# --- runt merge (both directions) -------------------------------------------


def test_chunk_text_runt_merge_final_chunk_backward() -> None:
    # Paragraphs sized so the last one alone would be a runt under
    # `minimum` -- it must merge into its predecessor.
    p1 = "A" * 90
    p2 = "B" * 90
    p3 = "C" * 10  # runt: below minimum=50
    text = "\n\n".join([p1, p2, p3])
    chunks = chunk_text(text, target=90, maximum=250, minimum=50)
    assert len(chunks) == 2
    last = chunks[-1]
    assert (last.char_end - last.char_start) >= 50
    assert text[last.char_start : last.char_end].strip().endswith("C" * 10)


def test_chunk_text_runt_merge_front_chunk_forward() -> None:
    # Force a tiny first paragraph via a huge `target` so the merge logic
    # for a leading runt (no predecessor) is exercised.
    p1 = "A" * 10  # runt: below minimum=50
    p2 = "B" * 90
    text = f"{p1}\n\n{p2}"
    chunks = chunk_text(text, target=1000, maximum=1000, minimum=50)
    assert len(chunks) == 1
    assert chunks[0].char_start == 0
    assert chunks[0].char_end == len(text)


def test_chunk_text_no_runt_merge_when_only_one_chunk() -> None:
    text = "Just one short paragraph."
    chunks = chunk_text(text, target=1800, maximum=2600, minimum=400)
    assert len(chunks) == 1


# --- split_paragraphs --------------------------------------------------------


def test_split_paragraphs_basic() -> None:
    text = "Para one.\n\nPara two.\n\nPara three."
    spans = split_paragraphs(text)
    assert [text[s:e] for s, e in spans] == ["Para one.", "Para two.", "Para three."]


def test_split_paragraphs_collapses_multiple_blank_lines() -> None:
    text = "Para one.\n\n\n\n\nPara two."
    spans = split_paragraphs(text)
    assert [text[s:e] for s, e in spans] == ["Para one.", "Para two."]


def test_split_paragraphs_drops_whitespace_only_paragraphs() -> None:
    text = "\n\n   \n\nReal paragraph.\n\n"
    spans = split_paragraphs(text)
    assert [text[s:e] for s, e in spans] == ["Real paragraph."]


def test_split_paragraphs_empty_text() -> None:
    assert split_paragraphs("") == []


# --- split_sentences edge cases ----------------------------------------------


def test_split_sentences_empty_text() -> None:
    assert split_sentences("") == []


def test_split_sentences_no_terminal_punctuation() -> None:
    text = "no terminal punctuation here just words"
    spans = split_sentences(text)
    assert len(spans) == 1
    assert text[spans[0][0] : spans[0][1]] == text


def test_split_sentences_offset_is_applied() -> None:
    text = "First. Second."
    spans = split_sentences(text, offset=100)
    assert spans[0][0] >= 100
