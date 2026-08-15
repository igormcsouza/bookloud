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
from src.contexts.library.domain.storage import PdfStorage, PresignedUpload
from src.contexts.library.domain.value_objects import BookStatus
from src.contexts.library.infrastructure.s3_keys import source_pdf_key
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
    def __init__(
        self, book_repository: BookRepository, chunk_repository: ChunkRepository
    ) -> None:
        self._book_repository = book_repository
        self._chunk_repository = chunk_repository

    def execute(self, user_id: str, book_id: str) -> None:
        _load_owned_book(self._book_repository, user_id, book_id)
        # Chunks first, then the book: DynamoDB has no cascade delete, and
        # leaving BOOK#<id>/CHUNK#* orphans behind after the book row is
        # gone would only surface much later (phase 6).
        self._chunk_repository.delete_for_book(book_id)
        self._book_repository.delete(user_id, book_id)
