"""S3 key layout for uploaded source PDFs (PLANS/phase-3.md §5.1).

**``SOURCE_PREFIX`` must stay in lockstep with ``infra/stacks/config.py``'s
``Config.SOURCE_PDF_PREFIX`` and ``local/setup.sh``** -- the same
"separately deployed projects, cannot share an import" rule
``infrastructure/keys.py`` already carries for the DynamoDB PK/SK constants.

Why the key carries both the user id and the book id: an S3 event only
gives the extract Lambda a bucket + key, and encoding both ids in the key
avoids a ``HeadObject`` round trip just to recover them. Why it's safe: the
presigned POST pins the *exact* key (no ``${filename}``, no ``starts-with``
condition -- see ``infrastructure/s3_pdf_storage.py``), so a browser
physically cannot write into another user's prefix. The extract Lambda
still re-validates by loading the book and comparing ``book.source_key`` to
the event key (``application/extraction.py``'s ``ExtractBook``, §8.2).
"""

from __future__ import annotations

import re

from src.contexts.library.infrastructure.keys import CHUNK_INDEX_WIDTH

SOURCE_PREFIX = "books/"
SOURCE_FILENAME = "source.pdf"

# PLANS/phase-4.md §5.1. Both buckets are dedicated (audio_bucket/
# marks_bucket), but the keys are still prefixed: phase 5 puts the stitched
# artifacts (audio/<u>/<b>/book.mp3, marks/<u>/<b>/book.json) here without
# colliding with a chunk index, and a later "one bucket for everything"
# consolidation stays possible. Unlike SOURCE_PREFIX, these are NOT
# duplicated in infra/stacks/config.py -- nothing in CDK filters on them
# (no S3 notification on these buckets), so no lockstep comment is needed.
AUDIO_PREFIX = "audio/"
MARKS_PREFIX = "marks/"
AUDIO_EXTENSION = ".mp3"
MARKS_EXTENSION = ".json"
AUDIO_CONTENT_TYPE = "audio/mpeg"
MARKS_CONTENT_TYPE = "application/json"

# PLANS/phase-5.md §5.1 -- a deliberate, flagged rename of the `full.*` names
# phase-4 §5.1 had reserved: "full" is actively misleading for the PARTIAL
# case, which §3 makes a first-class, everyday outcome rather than an
# exception. Nothing was ever written under full.*, so this is a rename on
# paper only.
BOOK_AUDIO_FILENAME = "book.mp3"
BOOK_MANIFEST_FILENAME = "book.json"

_KEY_RE = re.compile(r"^books/(?P<user_id>[^/]+)/(?P<book_id>[^/]+)/source\.pdf$")

# CHUNK_INDEX_WIDTH comes from infrastructure/keys.py -- not re-declared as a
# hard-coded "06d" -- so the DynamoDB SK and the S3 key can never drift apart
# (keys.py documents the width as immutable once any data exists).
_AUDIO_KEY_RE = re.compile(
    rf"^audio/(?P<user_id>[^/]+)/(?P<book_id>[^/]+)/(?P<index>\d{{{CHUNK_INDEX_WIDTH}}})\.mp3$"
)
_MARKS_KEY_RE = re.compile(
    rf"^marks/(?P<user_id>[^/]+)/(?P<book_id>[^/]+)/(?P<index>\d{{{CHUNK_INDEX_WIDTH}}})\.json$"
)

# No collision risk with the chunk regexes above: those require exactly
# CHUNK_INDEX_WIDTH *digits* before the extension, so parse_chunk_audio_key
# ("audio/u/b/book.mp3") is None and vice versa. Cross-rejection is tested.
_BOOK_AUDIO_KEY_RE = re.compile(
    rf"^audio/(?P<user_id>[^/]+)/(?P<book_id>[^/]+)/{re.escape(BOOK_AUDIO_FILENAME)}$"
)
_BOOK_MANIFEST_KEY_RE = re.compile(
    rf"^marks/(?P<user_id>[^/]+)/(?P<book_id>[^/]+)/{re.escape(BOOK_MANIFEST_FILENAME)}$"
)


def source_pdf_key(user_id: str, book_id: str) -> str:
    return f"{SOURCE_PREFIX}{user_id}/{book_id}/{SOURCE_FILENAME}"


def parse_source_pdf_key(key: str) -> tuple[str, str] | None:
    """Returns ``(user_id, book_id)`` or ``None`` for a malformed/foreign
    key. Rejects anything that isn't an exact ``books/<user>/<book>/
    source.pdf`` match -- in particular, ``[^/]+`` cannot match a literal
    ``/``, so a path-traversal or extra-segment key never parses."""
    match = _KEY_RE.match(key)
    if match is None:
        return None
    return (match.group("user_id"), match.group("book_id"))


def chunk_audio_key(user_id: str, book_id: str, index: int) -> str:
    return f"{AUDIO_PREFIX}{user_id}/{book_id}/{index:0{CHUNK_INDEX_WIDTH}d}{AUDIO_EXTENSION}"


def chunk_marks_key(user_id: str, book_id: str, index: int) -> str:
    return f"{MARKS_PREFIX}{user_id}/{book_id}/{index:0{CHUNK_INDEX_WIDTH}d}{MARKS_EXTENSION}"


def parse_chunk_audio_key(key: str) -> tuple[str, str, int] | None:
    """Returns ``(user_id, book_id, index)`` or ``None`` for a malformed/
    foreign key -- same ``[^/]+``-can't-match-``/`` traversal defence as
    ``parse_source_pdf_key``."""
    match = _AUDIO_KEY_RE.match(key)
    if match is None:
        return None
    return (match.group("user_id"), match.group("book_id"), int(match.group("index")))


def parse_chunk_marks_key(key: str) -> tuple[str, str, int] | None:
    match = _MARKS_KEY_RE.match(key)
    if match is None:
        return None
    return (match.group("user_id"), match.group("book_id"), int(match.group("index")))


def book_audio_key(user_id: str, book_id: str) -> str:
    """The stitched book-level MP3 (PLANS/phase-5.md §5.1). A pure function
    of ``(user_id, book_id)`` -- which is what makes the stitcher's S3 writes
    idempotent under duplicate delivery: every retry overwrites, never
    appends."""
    return f"{AUDIO_PREFIX}{user_id}/{book_id}/{BOOK_AUDIO_FILENAME}"


def book_manifest_key(user_id: str, book_id: str) -> str:
    return f"{MARKS_PREFIX}{user_id}/{book_id}/{BOOK_MANIFEST_FILENAME}"


def parse_book_audio_key(key: str) -> tuple[str, str] | None:
    match = _BOOK_AUDIO_KEY_RE.match(key)
    if match is None:
        return None
    return (match.group("user_id"), match.group("book_id"))


def parse_book_manifest_key(key: str) -> tuple[str, str] | None:
    match = _BOOK_MANIFEST_KEY_RE.match(key)
    if match is None:
        return None
    return (match.group("user_id"), match.group("book_id"))
