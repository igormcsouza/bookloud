"""PK/SK construction + parsing for the Library context's single-table
access patterns.

**These constants must stay in lockstep with ``infra/stacks/storage_stack.py``
and ``local/setup.sh``** — the same "separately deployed projects, cannot
share an import" rule ``src/config.py`` already carries for env var names.

**``CHUNK_INDEX_WIDTH`` is immutable once any data exists.** The chunk index
is zero-padded to this width so the SK sorts correctly as a *string*:
unpadded, ``CHUNK#10`` would sort before ``CHUNK#2`` lexicographically,
scrambling playback order from chunk 10 onward. Width 6 supports up to
999,999 chunks (a 1000-page book runs to roughly 2,000), comfortably more
than any real book will ever need. Changing this width after any chunk has
been written would require rewriting every existing chunk item's SK.
"""

from __future__ import annotations

PK = "PK"
SK = "SK"

USER_PREFIX = "USER#"
BOOK_PREFIX = "BOOK#"
CHUNK_PREFIX = "CHUNK#"
CHUNK_INDEX_WIDTH = 6


def pk_user(user_id: str) -> str:
    return f"{USER_PREFIX}{user_id}"


def sk_book(book_id: str) -> str:
    return f"{BOOK_PREFIX}{book_id}"


def pk_book(book_id: str) -> str:
    return f"{BOOK_PREFIX}{book_id}"


def sk_chunk(index: int) -> str:
    return f"{CHUNK_PREFIX}{index:0{CHUNK_INDEX_WIDTH}d}"


def book_id_from_sk(sk: str) -> str:
    return sk.removeprefix(BOOK_PREFIX)


def chunk_index_from_sk(sk: str) -> int:
    return int(sk.removeprefix(CHUNK_PREFIX))
