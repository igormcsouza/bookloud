"""Word-timing alignment/estimation -- **PURE** (PLANS/phase-4.md §7.5). No
``edge_tts``, no ``boto3``, no network -- string/number in, ``WordMark``s
out, so the part phase 6's highlight sync actually depends on is
exhaustively unit-testable, exactly as phase 3 did with
``domain/chunking.py``.

``iter_words`` is *the* tokenizer: both ``align_words`` (edge-tts's
measured path) and ``estimate_word_marks`` (Google's estimated path) must
agree on what a "word" is, or the two engines would produce mutually
incompatible marks files for what should be an interchangeable
``SynthesizedAudio.marks`` shape.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

from src.contexts.library.domain.synthesis import SynthesizedAudio, WordMark

MARKS_SCHEMA_VERSION = 1

# How far ahead of the cursor a `text.find` hit is still trusted as the
# "real" match rather than a desync -- see align_words's docstring.
_MAX_ALIGN_LOOKAHEAD = 200

_WORD_RE = re.compile(r"\S+")


def iter_words(text: str) -> Iterator[tuple[int, int, str]]:
    """``(char_start, char_end, word)`` for every whitespace-delimited token
    in ``text``. THE single tokenizer -- alignment and estimation must agree
    or the two engines produce mutually incompatible marks files."""
    for match in _WORD_RE.finditer(text):
        yield match.start(), match.end(), match.group()


def align_words(text: str, word_texts: Sequence[str]) -> list[tuple[int, int]]:
    """Map engine-reported word strings (edge-tts's ``WordBoundary`` events
    carry text but no offset into the input) back onto ``(char_start,
    char_end)`` ranges in ``text``.

    Algorithm: a forward cursor plus ``text.find(word, cursor)``. On a hit,
    emit ``(pos, pos + len(word))`` and advance the cursor to
    ``pos + len(word)``. On a miss, or a hit implausibly far ahead (more
    than ``_MAX_ALIGN_LOOKAHEAD`` chars past the cursor -- meaning we've
    desynchronized), emit ``(cursor, cursor + len(word))`` instead and
    advance the cursor by ``len(word)`` **without** consuming a real match,
    so one bad token can't cascade into every subsequent word being
    misaligned.

    The extractor already NFKC-normalizes the text and that exact text is
    what gets sent to the engine, so exact matches are overwhelmingly the
    norm; the fallback exists for punctuation-attachment and ligature edge
    cases.
    """
    cursor = 0
    result: list[tuple[int, int]] = []
    for word in word_texts:
        pos = text.find(word, cursor) if word else -1
        if pos != -1 and pos - cursor <= _MAX_ALIGN_LOOKAHEAD:
            result.append((pos, pos + len(word)))
            cursor = pos + len(word)
        else:
            result.append((cursor, cursor + len(word)))
            cursor += len(word)
    return result


def estimate_word_marks(
    text: str, *, duration_ms: int, anchors: Sequence[tuple[int, int]]
) -> list[WordMark]:
    """Interpolate each word's timing proportionally by its character
    position, using ``anchors`` (sorted ``(char_offset, time_ms)`` pairs,
    always including ``(0, 0)`` and ``(len(text), duration_ms)``) as the
    ground truth. Used by the Google adapter, whose only real timing signal
    is one SSML ``<mark>`` per sentence (PLANS/phase-4.md §7.4/§7.5)."""
    if not text or not anchors:
        return []

    anchor_chars = [a[0] for a in anchors]

    def _time_at(char_pos: int) -> float:
        # Find the anchor segment [lo, hi) containing char_pos via a manual
        # scan (anchors are few -- one per sentence -- so bisect would be
        # overkill ceremony for no real speed benefit here).
        lo_idx = 0
        for i in range(len(anchor_chars) - 1):
            if anchor_chars[i] <= char_pos:
                lo_idx = i
            else:
                break
        lo_char, lo_time = anchors[lo_idx]
        if lo_idx + 1 >= len(anchors):
            return float(lo_time)
        hi_char, hi_time = anchors[lo_idx + 1]
        if hi_char <= lo_char:
            return float(lo_time)
        fraction = (char_pos - lo_char) / (hi_char - lo_char)
        fraction = min(1.0, max(0.0, fraction))
        return lo_time + fraction * (hi_time - lo_time)

    marks: list[WordMark] = []
    for char_start, char_end, word in iter_words(text):
        start_time = _time_at(char_start)
        end_time = _time_at(char_end)
        offset_ms = max(0, round(start_time))
        end_ms = max(offset_ms, round(end_time))
        marks.append(
            WordMark(
                text=word,
                offset_ms=offset_ms,
                duration_ms=end_ms - offset_ms,
                char_start=char_start,
                char_end=char_end,
            )
        )
    return marks


def build_marks_document(
    *,
    book_id: str,
    chunk_index: int,
    user_id: str,
    audio_key: str,
    char_start: int,
    char_end: int,
    audio: SynthesizedAudio,
) -> dict:
    """Build the marks JSON document written to ``marks_bucket`` (PLANS/
    phase-4.md §7.5's shape). ``words`` is sorted by ``t`` and asserted
    strictly non-decreasing -- phase 6's highlight sync is a
    ``bisect_right`` over ``t`` on every ``timeupdate`` event; a single
    out-of-order entry would silently corrupt the search.

    ``user_id`` is accepted (not just ``book_id``) to keep this function's
    signature symmetric with the other synthesis-path calls that need it
    for authorization/logging -- the document itself has no ``userId`` key
    (§7.5's schema is deliberately minimal; ``s3_keys.py``'s ``audioKey``
    already encodes it)."""
    del user_id
    words = sorted(audio.marks, key=lambda mark: mark.offset_ms)
    previous_t = -1
    for mark in words:
        assert mark.offset_ms >= previous_t, "marks must be sorted by non-decreasing t"
        previous_t = mark.offset_ms

    return {
        "version": MARKS_SCHEMA_VERSION,
        "bookId": book_id,
        "chunkIndex": chunk_index,
        "source": audio.source.value,
        "voice": audio.voice,
        "timing": audio.timing.value,
        "audioKey": audio_key,
        "charStart": char_start,
        "charEnd": char_end,
        "durationMs": audio.duration_ms,
        "wordCount": len(words),
        "words": [
            {
                "t": mark.offset_ms,
                "d": mark.duration_ms,
                "s": mark.char_start,
                "e": mark.char_end,
                "w": mark.text,
            }
            for mark in words
        ],
    }
