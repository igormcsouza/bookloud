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


CHAT_PREFIX = "CHAT#"
CHAT_QUOTA_PREFIX = "CHATQUOTA#"

def pk_user(user_id: str) -> str:
    return f"{USER_PREFIX}{user_id}"


def sk_book(book_id: str) -> str:
    return f"{BOOK_PREFIX}{book_id}"


def pk_book(book_id: str) -> str:
    return f"{BOOK_PREFIX}{book_id}"


def sk_chunk(index: int) -> str:
    """Zero-padded to 6 digits so lexicographical sort matches numeric sort."""
    return f"{CHUNK_PREFIX}{index:0{CHUNK_INDEX_WIDTH}d}"


def sk_chat(created_at: str, message_id: str) -> str:
    """``CHAT#<createdAt>#<msgId>`` -- a REFINEMENT of
    IMPLEMENTATION_PLAN.md's ``CHAT#<msgId>`` (PLANS/phase-7.md §7.1, OQ-6).

    A bare uuid4 sorts randomly, so "the last six messages" would mean
    reading the whole partition and sorting client-side on every question.
    The ISO-8601 UTC prefix makes the SK chronological, so recent history is
    one Query(ScanIndexForward=False, Limit=N). The uuid suffix keeps two
    messages written in the same microsecond distinct.

    ``created_at`` MUST be the fixed-width form SystemClock produces
    (``datetime.now(UTC).isoformat()``); a value that drops the microseconds
    or the offset would sort wrongly against one that doesn't.
    test_keys.py asserts monotonicity and width."""
    # Enforce width: if created_at lacks microseconds, it's shorter. 
    # Usually it's 32 chars: '2026-08-12T14:15:22.000000+00:00'. We assume the caller provides it.
    return f"{CHAT_PREFIX}{created_at}#{message_id}"


def sk_chat_quota(date: str) -> str:
    """CHATQUOTA#<YYYY-MM-DD>"""
    return f"{CHAT_QUOTA_PREFIX}{date}"


def book_id_from_sk(sk: str) -> str:
    return sk.removeprefix(BOOK_PREFIX)


def chunk_index_from_sk(sk: str) -> int:
    return int(sk.removeprefix(CHUNK_PREFIX))
