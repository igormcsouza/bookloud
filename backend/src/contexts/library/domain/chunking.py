"""Pure text -> chunk-boundary chunker. **No pymupdf, no boto3 import** --
string in, ``ChunkBoundary`` list out, so this is exhaustively unit-testable
without a PDF or AWS dependency (PLANS/phase-3.md §7.4).

Sizing rationale: ~1800 chars ~= 300 words ~= 2 minutes of speech -- the
granularity phase 4's parallel synthesis, phase 6's seek/highlight
resolution, and phase 7's chat context windows all want.

Boundary algorithm, in strict preference order:

1. **Paragraph-greedy.** Close the chunk when adding the next paragraph
   would exceed ``maximum``, or once past ``target`` at a paragraph
   boundary.
2. **Sentence split** when one paragraph alone exceeds ``maximum``. Regex
   with an abbreviation guard list (``Mr Mrs Ms Dr Prof St Jr Sr vs etc e.g
   i.e cf Fig No Vol Ch pp`` + single-capital initials).
3. **Whitespace split** when one sentence alone exceeds ``maximum``: break
   at the last whitespace at or before ``maximum``. Never mid-word.
4. **Runt merge.** A final chunk shorter than ``minimum`` merges into the
   previous one if that stays <= ``maximum`` (and symmetrically, a runt at
   the very front with no predecessor merges into the next chunk instead).
5. **Page boundaries are irrelevant to chunking** -- pages are recorded per
   chunk by the caller (``infrastructure/pymupdf_extractor.py`` +
   ``domain/extraction.py``'s ``page_range_for``), not respected as breaks
   here.

Invariants (each has a dedicated test in ``test_chunking.py``): chunks
exactly partition the text (``chunks[0].char_start == 0``, contiguous,
``chunks[-1].char_end == len(text)``); stored text is the raw slice (no
stripping -- the offsets are load-bearing for phase 6's highlight sync, so
trimming here would desync them); every emitted chunk is non-blank;
``chunk_text("")``/whitespace-only input -> ``[]`` (the caller turns that
into ``ExtractionFailure.NO_TEXT_LAYER``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

DEFAULT_TARGET_CHARS = 1800
DEFAULT_MAX_CHARS = 2600
DEFAULT_MIN_CHARS = 400

_PARAGRAPH_BREAK_RE = re.compile(r"\n\s*\n")
_SENTENCE_PUNCT = ".!?"
_SENTENCE_CLOSERS = "\"')]”’"

# Abbreviations that must NOT be treated as a sentence end when followed by
# whitespace -- matched against the tail of the text up to (and including)
# the period, case-insensitively, guarded so "sr." doesn't match inside a
# longer word (e.g. "csr.").
_ABBREVIATIONS = (
    "mr.",
    "mrs.",
    "ms.",
    "dr.",
    "prof.",
    "st.",
    "jr.",
    "sr.",
    "vs.",
    "etc.",
    "e.g.",
    "i.e.",
    "cf.",
    "fig.",
    "no.",
    "vol.",
    "ch.",
    "pp.",
)


@dataclass(frozen=True)
class ChunkBoundary:
    char_start: int
    char_end: int


def chunk_text(
    text: str,
    *,
    target: int = DEFAULT_TARGET_CHARS,
    maximum: int = DEFAULT_MAX_CHARS,
    minimum: int = DEFAULT_MIN_CHARS,
) -> list[ChunkBoundary]:
    if not text or not text.strip():
        return []

    paragraph_spans = split_paragraphs(text)
    if not paragraph_spans:
        return []
    paragraph_atoms = _atoms_from_trimmed_spans(paragraph_spans, 0, len(text))

    def split_oversized_paragraph(start: int, end: int) -> list[tuple[int, int]]:
        return _split_oversized_paragraph(text, start, end, target, maximum)

    raw = _greedy_group(paragraph_atoms, target, maximum, split_oversized_paragraph)
    raw = _merge_runts(raw, minimum, maximum)
    return [ChunkBoundary(start, end) for start, end in raw]


def split_paragraphs(text: str) -> list[tuple[int, int]]:
    """Trimmed ``(start, end)`` spans for each non-blank paragraph, split on
    blank-line separators (``\\n\\s*\\n``). Spans are trimmed of surrounding
    whitespace -- a purely whitespace "paragraph" (e.g. a run of blank
    lines) is dropped entirely, not emitted as an empty span. Callers that
    need a *contiguous* partition of the full text (``chunk_text``)
    reconstruct it from these boundaries via ``_atoms_from_trimmed_spans``.
    """
    if not text:
        return []
    spans: list[tuple[int, int]] = []
    pos = 0
    for match in _PARAGRAPH_BREAK_RE.finditer(text):
        _append_trimmed(text, pos, match.start(), spans)
        pos = match.end()
    _append_trimmed(text, pos, len(text), spans)
    return spans


def split_sentences(text: str, offset: int = 0) -> list[tuple[int, int]]:
    """Trimmed ``(start, end)`` sentence spans within ``text``, shifted by
    ``offset`` so a caller can pass a paragraph substring and get spans back
    in the full document's coordinate space.

    Splits on ``.``/``!``/``?`` followed by whitespace (or end of text),
    guarded against a fixed abbreviation list and single-capital initials
    (e.g. "J. K. Rowling") so as not to split mid-abbreviation.
    """
    if not text:
        return []
    n = len(text)
    boundaries = [0]
    i = 0
    while i < n:
        ch = text[i]
        if ch not in _SENTENCE_PUNCT:
            i += 1
            continue
        j = i + 1
        while j < n and (text[j] in _SENTENCE_PUNCT or text[j] in _SENTENCE_CLOSERS):
            j += 1
        is_boundary = j >= n or text[j].isspace()
        if is_boundary and not (ch == "." and _is_abbreviation(text, i)):
            boundaries.append(j)
            i = j
        else:
            i = j

    spans: list[tuple[int, int]] = []
    for idx, start in enumerate(boundaries):
        end = boundaries[idx + 1] if idx + 1 < len(boundaries) else n
        trimmed = _trim(text, start, end)
        if trimmed is not None:
            spans.append(trimmed)

    return [(start + offset, end + offset) for start, end in spans]


# --- internals ---------------------------------------------------------


def _trim(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if end > start else None


def _append_trimmed(text: str, start: int, end: int, spans: list[tuple[int, int]]) -> None:
    trimmed = _trim(text, start, end)
    if trimmed is not None:
        spans.append(trimmed)


def _is_abbreviation(text: str, period_index: int) -> bool:
    prefix = text[: period_index + 1]
    lowered = prefix.lower()
    for abbr in _ABBREVIATIONS:
        if lowered.endswith(abbr):
            start = len(prefix) - len(abbr)
            # Must be a whole token: the character before the abbreviation
            # (if any) must not itself be a letter -- otherwise "sr." would
            # match inside e.g. "wisr." (never a real word, but defensive).
            if start == 0 or not prefix[start - 1].isalpha():
                return True

    # Single capital initial, e.g. "J." in "J. K. Rowling".
    j = period_index
    k = j
    while k > 0 and text[k - 1].isalpha():
        k -= 1
    word = text[k:j]
    return len(word) == 1 and word.isupper()


def _atoms_from_trimmed_spans(
    spans: list[tuple[int, int]], region_start: int, region_end: int
) -> list[tuple[int, int]]:
    """Reconstruct a **contiguous** partition of ``[region_start,
    region_end)`` from trimmed, non-overlapping ``spans`` (as returned by
    ``split_paragraphs``/``split_sentences``): each atom absorbs the
    whitespace/separator that follows it (and the very first atom absorbs
    any leading whitespace too), so ``atom[i].end == atom[i+1].start`` and
    the whole region is covered with no gaps."""
    if not spans:
        return [(region_start, region_end)] if region_end > region_start else []
    n = len(spans)
    atoms: list[tuple[int, int]] = []
    for i in range(n):
        start = region_start if i == 0 else spans[i][0]
        end = spans[i + 1][0] if i + 1 < n else region_end
        atoms.append((start, end))
    return atoms


def _greedy_group(
    atoms: list[tuple[int, int]],
    target: int,
    maximum: int,
    split_oversized: Callable[[int, int], list[tuple[int, int]]],
) -> list[tuple[int, int]]:
    """Greedily accumulate contiguous ``atoms`` into groups: close the
    current group when adding the next atom would exceed ``maximum``, or
    once the group has reached ``target`` at an atom boundary. An atom that
    alone exceeds ``maximum`` is handed to ``split_oversized`` instead of
    being accumulated."""
    raw: list[tuple[int, int]] = []
    pending: list[int] | None = None

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            raw.append((pending[0], pending[1]))
            pending = None

    for atom_start, atom_end in atoms:
        if atom_end - atom_start > maximum:
            flush()
            raw.extend(split_oversized(atom_start, atom_end))
            continue

        if pending is None:
            pending = [atom_start, atom_end]
        elif atom_end - pending[0] > maximum:
            flush()
            pending = [atom_start, atom_end]
        else:
            pending[1] = atom_end

        if pending is not None and pending[1] - pending[0] >= target:
            flush()

    flush()
    return raw


def _split_oversized_paragraph(
    text: str, start: int, end: int, target: int, maximum: int
) -> list[tuple[int, int]]:
    sentence_spans = split_sentences(text[start:end], offset=start)
    if not sentence_spans:
        # No usable sentence boundary at all (e.g. no terminal punctuation)
        # -- fall straight back to whitespace splitting of the whole span.
        return _split_at_whitespace(text, start, end, maximum)
    sentence_atoms = _atoms_from_trimmed_spans(sentence_spans, start, end)

    def split_oversized_sentence(s_start: int, s_end: int) -> list[tuple[int, int]]:
        return _split_at_whitespace(text, s_start, s_end, maximum)

    return _greedy_group(sentence_atoms, target, maximum, split_oversized_sentence)


def _split_at_whitespace(text: str, start: int, end: int, maximum: int) -> list[tuple[int, int]]:
    """Mechanically cut ``text[start:end]`` into pieces no longer than
    ``maximum``, breaking at the last whitespace at or before the limit.
    Never mid-word -- unless a single "word" itself exceeds ``maximum``, in
    which case a hard cut at the limit is the only option."""
    chunks: list[tuple[int, int]] = []
    pos = start
    while end - pos > maximum:
        limit = pos + maximum
        cut = None
        for i in range(limit, pos, -1):
            if text[i - 1].isspace():
                cut = i
                break
        if cut is None:
            cut = limit
        chunks.append((pos, cut))
        pos = cut
    if end > pos:
        chunks.append((pos, end))
    return chunks


def _merge_runts(raw: list[tuple[int, int]], minimum: int, maximum: int) -> list[tuple[int, int]]:
    if len(raw) <= 1:
        return raw
    chunks = list(raw)

    # The final chunk, if too small, merges backward into its predecessor
    # (repeatedly -- merging can itself leave a new, still-short final
    # chunk if `minimum` is close to `maximum`).
    while len(chunks) > 1 and (chunks[-1][1] - chunks[-1][0]) < minimum:
        prev_start, _ = chunks[-2]
        _, last_end = chunks[-1]
        if last_end - prev_start > maximum:
            break
        chunks[-2] = (prev_start, last_end)
        chunks.pop()

    # Symmetrically: a runt at the very front (no predecessor to merge
    # into) merges forward into the next chunk instead.
    if len(chunks) > 1 and (chunks[0][1] - chunks[0][0]) < minimum:
        first_start, _ = chunks[0]
        _, second_end = chunks[1]
        if second_end - first_start <= maximum:
            chunks[1] = (first_start, second_end)
            chunks.pop(0)

    return chunks
