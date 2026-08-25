"""Library context use cases.

``_load_owned_book`` is the single enforcement point for §6 Rule 3: every use
case that touches chunks calls it **before** touching the chunk repository,
so a caller can never reach another user's chunks even though
``ChunkRepository`` itself has no user in its key. A missing book and
another user's book are indistinguishable here -- both raise
``NotFoundError`` (404), never a 403 -- so the API never leaks whether a
book id belongs to someone else (§6 Rule 2).

``CreateBook``/``CreateBookCommand`` (phase 2) are **superseded** by
``RequestBookUpload`` (PLANS/phase-3.md §4.4/§4.3 Q14), not kept alongside
it: the book id must be known *before* ``Book.create`` so the S3 source key
can be computed and stored in the same ``save()`` call, which
``CreateBook.execute`` (id generated internally) could not do without being
composed with the upload step anyway.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.contexts.library.application.commands import RequestBookUploadCommand
from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.repository import BookRepository, ChunkRepository
from src.contexts.library.domain.storage import ObjectStorage, PdfStorage, PresignedUpload
from src.contexts.library.domain.value_objects import BookStatus
from src.contexts.library.infrastructure.s3_keys import (
    book_audio_key,
    book_manifest_key,
    chunk_audio_key,
    chunk_marks_key,
    source_pdf_key,
)
from src.shared_kernel.application.ports import Clock, IdGenerator
from src.shared_kernel.domain.errors import ConflictError, NotFoundError

# A book may (re)request an upload URL only while it has no successfully
# extracted content yet -- once extraction has started/succeeded, re-issuing
# a new upload target would race the pipeline reading `book.source_key`.
_REISSUABLE_STATUSES = (BookStatus.UPLOADED, BookStatus.FAILED)


def _load_owned_book(book_repository: BookRepository, user_id: str, book_id: str) -> Book:
    import logging
    logger = logging.getLogger(__name__)
    book = book_repository.get(user_id, book_id)
    if book is None:
        raise NotFoundError(f"Book {book_id} for user {user_id} not found")
    return book


@dataclass(frozen=True)
class BookUpload:
    book: Book
    upload: PresignedUpload


class RequestBookUpload:
    """Creates the ``Book`` row and its presigned browser upload in one call
    (PLANS/phase-3.md §4.1/Q1): a bare ``POST /books`` with no PDF behind it
    is exactly the half-formed state phase 2 rejected. The book id is
    generated here (not inside ``Book.create``) so the S3 source key can be
    computed and persisted in the very same ``save()``."""

    def __init__(
        self,
        book_repository: BookRepository,
        pdf_storage: PdfStorage,
        clock: Clock,
        id_generator: IdGenerator,
    ) -> None:
        self._book_repository = book_repository
        self._pdf_storage = pdf_storage
        self._clock = clock
        self._id_generator = id_generator

    def execute(self, command: RequestBookUploadCommand) -> BookUpload:
        book_id = self._id_generator.new_id()
        key = source_pdf_key(command.user_id, book_id)
        book = Book.create(
            id=book_id,
            user_id=command.user_id,
            title_raw=command.title,
            now=self._clock.now(),
            source_key=key,
        )
        self._book_repository.save(book)
        upload = self._pdf_storage.presigned_upload(key=key)
        return BookUpload(book=book, upload=upload)


class ReissueBookUpload:
    """Re-issues a presigned upload for an existing book -- the retry/
    re-upload path (PLANS/phase-3.md §4.2), so a failed browser upload
    doesn't orphan the book row and force a duplicate ``POST /books``."""

    def __init__(self, book_repository: BookRepository, pdf_storage: PdfStorage) -> None:
        self._book_repository = book_repository
        self._pdf_storage = pdf_storage

    def execute(self, user_id: str, book_id: str) -> PresignedUpload:
        book = _load_owned_book(self._book_repository, user_id, book_id)
        if book.status not in _REISSUABLE_STATUSES:
            raise ConflictError("Book is already being processed")
        # Invariant since phase 3: every book is created together with its
        # source_key (RequestBookUpload sets it before the first save()) --
        # a None here would mean a pre-phase-3 row, which cannot exist.
        assert book.source_key is not None
        return self._pdf_storage.presigned_upload(key=book.source_key)


class GetBook:
    def __init__(self, book_repository: BookRepository) -> None:
        self._book_repository = book_repository

    def execute(self, user_id: str, book_id: str) -> Book:
        return _load_owned_book(self._book_repository, user_id, book_id)


class ListBooks:
    def __init__(self, book_repository: BookRepository) -> None:
        self._book_repository = book_repository

    def execute(self, user_id: str) -> list[Book]:
        return self._book_repository.list_for_user(user_id)


class ListBookChunks:
    def __init__(
        self, book_repository: BookRepository, chunk_repository: ChunkRepository
    ) -> None:
        self._book_repository = book_repository
        self._chunk_repository = chunk_repository

    def execute(self, user_id: str, book_id: str) -> list[Chunk]:
        _load_owned_book(self._book_repository, user_id, book_id)
        return self._chunk_repository.list_for_book(book_id)


class DeleteBook:
    """``DELETE /books/{id}`` (issue #12). Deletes whatever exists for the
    book -- no special-casing a mid-pipeline (``EXTRACTING``/``STITCHING``)
    book, mirroring how phase 5's sweeper already tolerates a stranded book
    row. Every S3 delete is idempotent (a missing key is not an error), so a
    book that never got past ``UPLOADED`` -- no chunks, no stitched audio --
    deletes cleanly with the marks/audio calls simply no-op-ing.

    **The book row is deleted FIRST**, before any chunk/S3 cleanup -- not
    last. Every pipeline worker's writes (extraction's ``EXTRACTING`` claim,
    synthesis/stitch's chunk ``update_status`` calls) are conditioned on
    ``attribute_exists(PK)`` against the book row they're claiming or the
    chunk row they're updating; deleting the book row up front means a
    worker racing this delete hits that condition and aborts with a
    ``NotFoundError``/``ConflictError`` instead of writing a new chunk row
    or S3 object *after* this method's own chunk snapshot (``list_for_book``
    below) was taken -- which would otherwise orphan it forever, since a
    retried DELETE 404s the instant the book row is gone and has no way to
    rediscover it. This does not close every race (a worker already past
    its own conditional claim can still finish a single in-flight write),
    but it eliminates the class of race that starts a *new* claim after
    this delete begins. The residual, narrower race is accepted here rather
    than adding a book-level delete lock, matching this issue's explicit
    "no special-casing" scope.

    Chunk rows and S3 objects are best-effort cleanup after that: a crash
    partway through leaves orphaned chunk rows/objects with no book row
    pointing at them, but nothing a caller can act on differently than "some
    storage was not reclaimed" -- there is no natural retry entry point once
    the book row (the only handle for reconstructing keys) is gone either
    way.
    """

    def __init__(
        self,
        book_repository: BookRepository,
        chunk_repository: ChunkRepository,
        pdf_storage: PdfStorage,
        audio_storage: ObjectStorage,
        marks_storage: ObjectStorage,
    ) -> None:
        self._book_repository = book_repository
        self._chunk_repository = chunk_repository
        self._pdf_storage = pdf_storage
        self._audio_storage = audio_storage
        self._marks_storage = marks_storage

    def execute(self, user_id: str, book_id: str) -> None:
        book = _load_owned_book(self._book_repository, user_id, book_id)
        chunks = self._chunk_repository.list_for_book(book_id)

        # First: see the docstring's race-window note above.
        self._book_repository.delete(user_id, book_id)

        if book.source_key is not None:
            self._pdf_storage.delete(key=book.source_key)

        # book_audio_key/book_manifest_key are gated the same way as
        # source_key above: a book that never got past UPLOADED has no
        # stitched output, so skip the no-op S3 calls rather than issuing
        # them unconditionally.
        if book.audio_key is not None:
            self._audio_storage.delete(key=book_audio_key(user_id, book_id))
        self._audio_storage.delete_many(
            keys=[chunk_audio_key(user_id, book_id, chunk.index) for chunk in chunks]
        )
        if book.manifest_key is not None:
            self._marks_storage.delete(key=book_manifest_key(user_id, book_id))
        self._marks_storage.delete_many(
            keys=[chunk_marks_key(user_id, book_id, chunk.index) for chunk in chunks]
        )

        self._chunk_repository.delete_for_book(book_id)
