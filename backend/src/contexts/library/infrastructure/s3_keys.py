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

SOURCE_PREFIX = "books/"
SOURCE_FILENAME = "source.pdf"

_KEY_RE = re.compile(r"^books/(?P<user_id>[^/]+)/(?P<book_id>[^/]+)/source\.pdf$")


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
