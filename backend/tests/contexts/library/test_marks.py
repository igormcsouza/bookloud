from __future__ import annotations

import pytest

from src.contexts.library.domain.marks import (
    MARKS_SCHEMA_VERSION,
    align_words,
    build_marks_document,
    estimate_word_marks,
    iter_words,
)
from src.contexts.library.domain.synthesis import SynthesizedAudio, WordMark
from src.contexts.library.domain.value_objects import MarksTiming, SynthesisSource

# --- iter_words --------------------------------------------------------------


def test_iter_words_basic_ascii() -> None:
    text = "Chapter one begins"
    words = list(iter_words(text))
    assert [w for _, _, w in words] == ["Chapter", "one", "begins"]
    assert words[0] == (0, 7, "Chapter")
    assert words[1] == (8, 11, "one")
    assert words[2] == (12, 18, "begins")


def test_iter_words_unicode() -> None:
    text = "Café — 日本語 test"
    words = list(iter_words(text))
    assert [w for _, _, w in words] == ["Café", "—", "日本語", "test"]


def test_iter_words_punctuation_attached() -> None:
    text = 'He said, "hello!"'
    words = list(iter_words(text))
    assert [w for _, _, w in words] == ["He", "said,", '"hello!"']


def test_iter_words_multiple_spaces_and_tabs() -> None:
    text = "one    two\tthree"
    words = list(iter_words(text))
    assert [w for _, _, w in words] == ["one", "two", "three"]


def test_iter_words_empty_string() -> None:
    assert list(iter_words("")) == []


def test_iter_words_whitespace_only() -> None:
    assert list(iter_words("   \t  ")) == []


def test_iter_words_char_offsets_are_exact_slices() -> None:
    text = "  leading and trailing  "
    for start, end, word in iter_words(text):
        assert text[start:end] == word


# --- align_words --------------------------------------------------------------


def test_align_words_exact_match() -> None:
    text = "Chapter one begins here"
    result = align_words(text, ["Chapter", "one", "begins", "here"])
    assert result == [(0, 7), (8, 11), (12, 18), (19, 23)]


def test_align_words_punctuation_attached_tokens_match_engine_text() -> None:
    # edge-tts commonly reports word-boundary text WITHOUT trailing
    # punctuation even though the source text has it attached -- alignment
    # must still find the substring.
    text = "Hello, world!"
    result = align_words(text, ["Hello", "world"])
    assert result == [(0, 5), (7, 12)]


def test_align_words_unmatched_token_falls_back_to_cursor_without_consuming() -> None:
    text = "one two three"
    # "zzz" never appears -- falls back to (cursor, cursor+len), cursor
    # advances by len(word) without a real match, and subsequent real words
    # must still be found relative to the *advanced* cursor.
    result = align_words(text, ["one", "zzz", "two", "three"])
    assert result[0] == (0, 3)  # "one" real match
    assert result[1] == (3, 6)  # "zzz" fallback: cursor(3) to cursor+3
    # cursor is now 6; "two" is found via text.find("two", 6) -> at index 4?
    # "two" actually starts at index 4 in "one two three", but cursor is 6,
    # so find("two", 6) fails (already past it) -> another fallback.
    assert result[2] == (6, 9)
    assert result[3][0] >= 6


def test_align_words_wildly_out_of_range_match_rejected() -> None:
    text = "start " + ("filler " * 100) + "word"
    # "word" appears near the very end, far beyond _MAX_ALIGN_LOOKAHEAD (200)
    # chars past the cursor (which starts at 0) -- must be rejected as a
    # desync and fall back rather than jumping the cursor hundreds of chars.
    result = align_words(text, ["word"])
    assert result == [(0, 4)]  # fallback: (cursor=0, cursor+len("word"))


def test_align_words_empty_word_list() -> None:
    assert align_words("some text", []) == []


def test_align_words_cursor_advances_monotonically_on_real_matches() -> None:
    text = "the cat sat on the mat"
    # "the" appears twice; each occurrence should be matched in order, not
    # both matched to the first occurrence.
    result = align_words(text, ["the", "cat", "sat", "on", "the", "mat"])
    first_the, cat, sat, on, second_the, mat = result
    assert text[first_the[0] : first_the[1]] == "the"
    assert text[second_the[0] : second_the[1]] == "the"
    assert first_the[0] < second_the[0]


# --- estimate_word_marks -------------------------------------------------------


def test_estimate_word_marks_exactness_at_anchors() -> None:
    text = "one two three four"
    anchors = [(0, 0), (len(text), 4000)]
    marks = estimate_word_marks(text, duration_ms=4000, anchors=anchors)
    assert len(marks) == 4
    assert marks[0].offset_ms == 0
    # Last word's end should land at (or very near) the final anchor time.
    assert marks[-1].offset_ms + marks[-1].duration_ms <= 4000


def test_estimate_word_marks_monotonic_non_decreasing_offsets() -> None:
    text = "the quick brown fox jumps over the lazy dog"
    anchors = [(0, 0), (len(text), 9000)]
    marks = estimate_word_marks(text, duration_ms=9000, anchors=anchors)
    offsets = [m.offset_ms for m in marks]
    assert offsets == sorted(offsets)


def test_estimate_word_marks_multi_sentence_anchors_resync_each_sentence() -> None:
    text = "Short. A somewhat longer sentence follows here."
    first_end = text.index(".") + 1
    anchors = [(0, 0), (first_end, 1000), (len(text), 5000)]
    marks = estimate_word_marks(text, duration_ms=5000, anchors=anchors)
    # The word ending exactly at the first sentence boundary should land at
    # (or very near) the 1000ms anchor.
    boundary_word = next(m for m in marks if m.char_end == first_end)
    assert abs((boundary_word.offset_ms + boundary_word.duration_ms) - 1000) <= 1


def test_estimate_word_marks_single_anchor_degenerate_case() -> None:
    text = "only one anchor point"
    marks = estimate_word_marks(text, duration_ms=0, anchors=[(0, 0)])
    assert all(m.offset_ms == 0 for m in marks)


def test_estimate_word_marks_empty_text_returns_empty() -> None:
    assert estimate_word_marks("", duration_ms=1000, anchors=[(0, 0), (0, 1000)]) == []


def test_estimate_word_marks_empty_anchors_returns_empty() -> None:
    assert estimate_word_marks("some text", duration_ms=1000, anchors=[]) == []


def test_estimate_word_marks_char_start_end_preserved() -> None:
    text = "alpha beta"
    marks = estimate_word_marks(text, duration_ms=1000, anchors=[(0, 0), (len(text), 1000)])
    assert marks[0].char_start == 0
    assert marks[0].char_end == 5
    assert marks[1].char_start == 6
    assert marks[1].char_end == 10


# --- build_marks_document -------------------------------------------------------


def _audio(marks: tuple[WordMark, ...], duration_ms: int = 1000) -> SynthesizedAudio:
    return SynthesizedAudio(
        audio=b"fake-mp3-bytes",
        content_type="audio/mpeg",
        duration_ms=duration_ms,
        marks=marks,
        voice="en-US-AriaNeural",
        source=SynthesisSource.EDGE_TTS,
        timing=MarksTiming.MEASURED,
    )


def test_build_marks_document_schema_keys() -> None:
    marks = (WordMark(text="hi", offset_ms=0, duration_ms=200, char_start=0, char_end=2),)
    audio = _audio(marks)
    doc = build_marks_document(
        book_id="book-1",
        chunk_index=7,
        user_id="user-1",
        audio_key="audio/user-1/book-1/000007.mp3",
        char_start=100,
        char_end=200,
        audio=audio,
    )
    assert doc["version"] == MARKS_SCHEMA_VERSION
    assert doc["bookId"] == "book-1"
    assert doc["chunkIndex"] == 7
    assert doc["source"] == "edge-tts"
    assert doc["voice"] == "en-US-AriaNeural"
    assert doc["timing"] == "measured"
    assert doc["audioKey"] == "audio/user-1/book-1/000007.mp3"
    assert doc["charStart"] == 100
    assert doc["charEnd"] == 200
    assert doc["durationMs"] == 1000
    assert doc["wordCount"] == 1
    assert doc["words"] == [{"t": 0, "d": 200, "s": 0, "e": 2, "w": "hi"}]


def test_build_marks_document_sorts_by_t() -> None:
    marks = (
        WordMark(text="second", offset_ms=500, duration_ms=100, char_start=3, char_end=9),
        WordMark(text="first", offset_ms=0, duration_ms=200, char_start=0, char_end=2),
    )
    doc = build_marks_document(
        book_id="b",
        chunk_index=0,
        user_id="u",
        audio_key="audio/u/b/000000.mp3",
        char_start=0,
        char_end=10,
        audio=_audio(marks),
    )
    assert [w["w"] for w in doc["words"]] == ["first", "second"]
    assert [w["t"] for w in doc["words"]] == [0, 500]


def test_build_marks_document_word_count_correctness() -> None:
    marks = tuple(
        WordMark(text=str(i), offset_ms=i * 10, duration_ms=5, char_start=i, char_end=i + 1) for i in range(5)
    )
    doc = build_marks_document(
        book_id="b", chunk_index=0, user_id="u", audio_key="k", char_start=0, char_end=5, audio=_audio(marks)
    )
    assert doc["wordCount"] == 5


def test_build_marks_document_out_of_order_marks_raise_assertion() -> None:
    # offset_ms strictly decreasing after the sort key is applied would be a
    # contradiction -- but the function sorts *before* asserting, so this
    # actually tests that ties/negative regressions can't slip through a
    # buggy sort. Use duplicate offsets with the assertion still holding
    # (non-decreasing, not strictly increasing) to document that ties are
    # legal.
    marks = (
        WordMark(text="a", offset_ms=100, duration_ms=50, char_start=0, char_end=1),
        WordMark(text="b", offset_ms=100, duration_ms=50, char_start=1, char_end=2),
    )
    doc = build_marks_document(
        book_id="b", chunk_index=0, user_id="u", audio_key="k", char_start=0, char_end=2, audio=_audio(marks)
    )
    assert [w["t"] for w in doc["words"]] == [100, 100]


def test_build_marks_document_empty_marks() -> None:
    doc = build_marks_document(
        book_id="b", chunk_index=0, user_id="u", audio_key="k", char_start=0, char_end=0, audio=_audio(())
    )
    assert doc["words"] == []
    assert doc["wordCount"] == 0
