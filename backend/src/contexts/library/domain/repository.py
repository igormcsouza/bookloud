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
from dataclasses import dataclass
from typing import Protocol

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.value_objects import BookStatus, ChunkStatus


@dataclass(frozen=True)
class ChunkCounters:
    """Returned by ``increment_chunks_done`` off the same ``UpdateItem``
    round trip (``ReturnValues="ALL_NEW"``) -- reading the book again
    afterwards would be a race (two workers could both read after both
    increments and both think they're last, or neither). Phase 4 only uses
    this for structured logging; phase 5's stitcher trigger uses
    ``is_complete`` with zero extra read (PLANS/phase-4.md §5.4)."""

    chunks_done: int
    chunks_total: int
    chunks_failed: int

    @property
    def is_complete(self) -> bool:
        # chunks_total > 0 guards the "publish happened before the flip"
        # class of bug (§4.2) from silently reading as "complete".
        return self.chunks_total > 0 and self.chunks_done >= self.chunks_total


class BookRepository(Protocol):
    def save(self, book: Book) -> None: ...  # pragma: no cover

    def get(self, user_id: str, book_id: str) -> Book | None: ...  # pragma: no cover

    def list_for_user(self, user_id: str) -> list[Book]: ...  # pragma: no cover

    def delete(self, user_id: str, book_id: str) -> None: ...  # pragma: no cover

    def get_manifest_key(self, book_id: str) -> str | None: ...  # pragma: no cover
    def get_audio_key(self, book_id: str) -> str | None: ...  # pragma: no cover

    def update_status(
        self,
        user_id: str,
        book_id: str,
        status: BookStatus,
        *,
        expected_statuses: Sequence[BookStatus] | None = None,
        chunks_total: int | None = None,
        chunks_done: int | None = None,
        chunks_failed: int | None = None,
        page_count: int | None = None,
        audio_key: str | None = None,
        manifest_key: str | None = None,
        audio_duration_ms: int | None = None,
        clear_stitch_outputs: bool = False,
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
        - ``chunks_done``/``chunks_failed`` (PLANS/phase-4.md §5.4) let the
          ``EXTRACTED`` flip reset both counters to 0 -- without this,
          re-extracting a previously-``FAILED`` book (``FAILED`` is in
          ``_CLAIMABLE_STATUSES``) leaves a stale ``chunksDone`` and the
          fan-in arithmetic is wrong forever.
        - ``audio_key``/``manifest_key``/``audio_duration_ms`` (PLANS/
          phase-5.md §5.3) are what the stitch Lambda's terminal transition
          writes alongside ``READY``/``PARTIAL``.
        - ``clear_stitch_outputs`` REMOVEs ``audioKey``/``manifestKey`` and
          zeroes ``audioDurationMs``. Used by ``ExtractBook``'s ``EXTRACTED``
          flip, for the same reason it resets the counters: re-extracting a
          previously-stitched book must not leave it advertising audio for
          text that no longer exists. ``audio_key`` together with
          ``clear_stitch_outputs=True`` is a programming error ->
          ``ValueError``, mirroring the ``failure_reason`` guard.
        """
        ...  # pragma: no cover

    def increment_chunks_done(
        self, user_id: str, book_id: str, *, failed: bool = False
    ) -> ChunkCounters:
        """Atomic ``ADD chunksDone :one`` (``+ chunksFailed :one`` when
        ``failed=True``), ``ReturnValues="ALL_NEW"`` -- returns the whole
        post-update counters in the same round trip so the caller can answer
        "is this the last chunk?" with zero extra read (PLANS/phase-4.md
        §5.4)."""
        ...  # pragma: no cover


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
        expected_statuses: Sequence[ChunkStatus] | None = None,
        audio_key: str | None = None,
        marks_key: str | None = None,
        duration_ms: int | None = None,
        synthesis_source: str | None = None,
        failure_reason: str | None = None,
        clear_failure_reason: bool = False,
    ) -> None:
        """The same generalization phase 3 gave ``BookRepository.update_status``
        (PLANS/phase-4.md §5.3) -- ``expected_statuses`` makes the synthesize
        Lambda's claim (``-> SYNTHESIZING``) and terminal transitions
        (``-> DONE``/``-> FAILED``) atomic and conflict-disambiguated exactly
        like the book claim. On a ``ConditionalCheckFailedException`` the
        adapter re-``get``s: absent -> ``NotFoundError`` (preserves phase-3
        behaviour), present -> ``ConflictError`` -- the single most important
        line in phase 4 (§8.4's exactly-once counter gate). ``failure_reason``
        together with ``clear_failure_reason=True`` is a programming error ->
        ``ValueError``. Every phase-3 call site (no ``expected_statuses``,
        just ``audio_key``/``marks_key``) keeps working unchanged."""
        ...  # pragma: no cover

    def delete_for_book(self, book_id: str) -> int: ...  # pragma: no cover


class ChatRepository(Protocol):
    """``book_id`` must already have been authorized via
    ``BookRepository.get(user_id, book_id)``. This port performs no access
    control -- the same rule ``ChunkRepository`` carries, for the same
    structural reason (PLANS/phase-7.md §7.3)."""
    def list_messages(self, book_id: str, limit: int = 50) -> list['ChatMessage']: ...  # pragma: no cover
    def save_turn(self, user_msg: 'ChatMessage', assistant_msg: 'ChatMessage') -> None: ...  # pragma: no cover
    def clear(self, book_id: str) -> None: ...  # pragma: no cover


class ChatQuotaRepository(Protocol):
    def increment_and_check(self, user_id: str, date: str, limit: int) -> bool:
        """Returns True if successful, False if the limit was already reached."""
        ...  # pragma: no cover
    def get_count(self, user_id: str, date: str) -> int: ...  # pragma: no cover
