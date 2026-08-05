"""Repository ports for the Library context — ``Protocol``s, not ABCs
(jgautocar's convention: adapters satisfy these structurally, no
inheritance). Every method body is ``...`` (``# pragma: no cover``),
matching ``shared_kernel/application/ports.py``.

**``save()`` vs the targeted-update methods — a real trap, read this before
calling either.** ``save()`` is a whole-item ``put_item``: it is for
*creation* and owner-initiated whole-record edits only. Running it
concurrently with phase 4's fan-in (``increment_chunks_done``) would clobber
``chunksDone`` with a stale in-memory value. Every status/counter mutation
must instead go through the targeted-update methods
(``update_status``/``increment_chunks_done``), which use a scoped
``UpdateExpression`` and never touch fields they don't intend to change.

Both targeted-update methods carry ``ConditionExpression="attribute_exists(PK)"``
and raise ``NotFoundError`` when it fails (``ConditionalCheckFailedException``).
Without this, DynamoDB's ``SET``/``ADD`` **upserts** — an update for a
deleted book/chunk would silently resurrect a half-formed item. This is a
genuine correctness bug the tests must guard against, not a nicety.

**``ChunkRepository`` performs no access control.** Its methods take a bare
``book_id: str`` with no user in the key (chunk items are keyed
``PK=BOOK#<bookId>``, which structurally cannot carry ownership). Every
caller **must** already have authorized ``book_id`` via
``BookRepository.get(user_id, book_id)`` before calling any method here —
see ``application/use_cases.py``'s ``_load_owned_book`` helper, which is the
single place this rule is enforced.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.value_objects import BookStatus, ChunkStatus


class BookRepository(Protocol):
    def save(self, book: Book) -> None: ...  # pragma: no cover

    def get(self, user_id: str, book_id: str) -> Book | None: ...  # pragma: no cover

    def list_for_user(self, user_id: str) -> list[Book]: ...  # pragma: no cover

    def delete(self, user_id: str, book_id: str) -> None: ...  # pragma: no cover

    def update_status(
        self,
        user_id: str,
        book_id: str,
        status: BookStatus,
        *,
        expected_statuses: Sequence[BookStatus] | None = None,
        chunks_total: int | None = None,
        page_count: int | None = None,
        failure_reason: str | None = None,
        clear_failure_reason: bool = False,
        updated_at: str | None = None,
    ) -> None:
        """One generalized targeted update rather than three bolted-on
        methods (PLANS/phase-3.md §5.3) -- extraction needs to set status +
        chunksTotal + pageCount + updatedAt atomically, and to *clear*
        failureReason on retry.

        - ``expected_statuses``, when given, ANDs an additional
          ``#status IN (...)`` condition onto the existing
          ``attribute_exists(PK)`` check -- this is what makes the extract
          Lambda's atomic ``EXTRACTING`` claim usable (§8.2): the claim only
          succeeds when the book is currently ``UPLOADED``/``FAILED``.
        - On a ``ConditionalCheckFailedException``, the adapter
          disambiguates: re-``get_item``, item absent -> ``NotFoundError``
          (preserving phase-2 behaviour), item present -> ``ConflictError``
          (409) -- so the caller can tell "book is gone" from "book is
          already claimed by another invocation".
        - ``failure_reason`` set together with ``clear_failure_reason=True``
          is a programming error -> ``ValueError`` (not a domain error;
          nothing an end user did wrong).
        - Every phase-2 call site (``update_status(u, b, EXTRACTED)`` with
          no kwargs) keeps working unchanged.
        """
        ...  # pragma: no cover

    def increment_chunks_done(self, user_id: str, book_id: str) -> int: ...  # pragma: no cover


class ChunkRepository(Protocol):
    """``book_id`` must already have been authorized via
    ``BookRepository.get(user_id, book_id)``. This port performs no access
    control."""

    def save(self, chunk: Chunk) -> None: ...  # pragma: no cover

    def save_all(self, chunks: Sequence[Chunk]) -> None: ...  # pragma: no cover

    def get(self, book_id: str, index: int) -> Chunk | None: ...  # pragma: no cover

    def list_for_book(self, book_id: str) -> list[Chunk]: ...  # pragma: no cover

    def update_status(
        self,
        book_id: str,
        index: int,
        status: ChunkStatus,
        *,
        audio_key: str | None = None,
        marks_key: str | None = None,
    ) -> None: ...  # pragma: no cover

    def delete_for_book(self, book_id: str) -> int: ...  # pragma: no cover
