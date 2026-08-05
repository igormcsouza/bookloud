"""Library context use cases.

``_load_owned_book`` is the single enforcement point for §6 Rule 3: every use
case that touches chunks calls it **before** touching the chunk repository,
so a caller can never reach another user's chunks even though
``ChunkRepository`` itself has no user in its key. A missing book and
another user's book are indistinguishable here -- both raise
``NotFoundError`` (404), never a 403 -- so the API never leaks whether a
book id belongs to someone else (§6 Rule 2).
"""

from __future__ import annotations

from src.contexts.library.application.commands import CreateBookCommand
from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.repository import BookRepository, ChunkRepository
from src.shared_kernel.application.ports import Clock, IdGenerator
from src.shared_kernel.domain.errors import NotFoundError


def _load_owned_book(book_repository: BookRepository, user_id: str, book_id: str) -> Book:
    book = book_repository.get(user_id, book_id)
    if book is None:
        raise NotFoundError("Book not found")
    return book


class CreateBook:
    """Not wired to any HTTP route in Phase 2 -- phase 3's presigned-POST
    controller calls this. Built and unit-tested now so book creation logic
    isn't invented twice."""

    def __init__(
        self,
        book_repository: BookRepository,
        clock: Clock,
        id_generator: IdGenerator,
    ) -> None:
        self._book_repository = book_repository
        self._clock = clock
        self._id_generator = id_generator

    def execute(self, command: CreateBookCommand) -> Book:
        book = Book.create(
            id=self._id_generator.new_id(),
            user_id=command.user_id,
            title_raw=command.title,
            now=self._clock.now(),
        )
        self._book_repository.save(book)
        return book


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
