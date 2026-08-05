from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.contexts.library.application.commands import RequestBookUploadCommand
from src.contexts.library.application.use_cases import (
    DeleteBook,
    GetBook,
    ListBookChunks,
    ListBooks,
    ReissueBookUpload,
    RequestBookUpload,
)
from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.storage import PresignedUpload
from src.contexts.library.domain.value_objects import BookStatus
from src.shared_kernel.domain.errors import ConflictError, NotFoundError

FIXED_NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return FIXED_NOW


class SequentialIdGenerator:
    def __init__(self) -> None:
        self._n = 0

    def new_id(self) -> str:
        self._n += 1
        return f"id-{self._n}"


class FakeBookRepository:
    """In-memory fake keyed by (user_id, book_id) -- structurally cannot
    return another user's book, mirroring the real repository's isolation
    guarantee."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], Book] = {}

    def save(self, book: Book) -> None:
        self._store[(book.user_id, book.id)] = book

    def get(self, user_id: str, book_id: str) -> Book | None:
        return self._store.get((user_id, book_id))

    def list_for_user(self, user_id: str) -> list[Book]:
        books = [b for (uid, _), b in self._store.items() if uid == user_id]
        books.sort(key=lambda b: b.created_at, reverse=True)
        return books

    def delete(self, user_id: str, book_id: str) -> None:
        self._store.pop((user_id, book_id), None)

    def update_status(self, user_id: str, book_id: str, status, **kwargs) -> None:
        raise NotImplementedError

    def increment_chunks_done(self, user_id: str, book_id: str) -> int:
        raise NotImplementedError


class SpyChunkRepository:
    """Records every call so tests can assert it was never invoked (the
    access-control ordering test) or invoked in a particular order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._chunks: dict[str, list[Chunk]] = {}

    def save(self, chunk: Chunk) -> None:
        self.calls.append(("save", (chunk,)))
        self._chunks.setdefault(chunk.book_id, []).append(chunk)

    def save_all(self, chunks) -> None:
        self.calls.append(("save_all", (chunks,)))
        for chunk in chunks:
            self._chunks.setdefault(chunk.book_id, []).append(chunk)

    def get(self, book_id: str, index: int):
        self.calls.append(("get", (book_id, index)))
        return next(
            (c for c in self._chunks.get(book_id, []) if c.index == index), None
        )

    def list_for_book(self, book_id: str) -> list[Chunk]:
        self.calls.append(("list_for_book", (book_id,)))
        return list(self._chunks.get(book_id, []))

    def update_status(self, book_id, index, status, *, audio_key=None, marks_key=None) -> None:
        self.calls.append(("update_status", (book_id, index, status)))

    def delete_for_book(self, book_id: str) -> int:
        self.calls.append(("delete_for_book", (book_id,)))
        count = len(self._chunks.pop(book_id, []))
        return count


class FakePdfStorage:
    """Records every ``presigned_upload`` key requested."""

    def __init__(self) -> None:
        self.presigned_calls: list[str] = []

    def presigned_upload(self, *, key: str) -> PresignedUpload:
        self.presigned_calls.append(key)
        return PresignedUpload(
            url="https://bucket.s3.amazonaws.com/",
            fields={"key": key, "Content-Type": "application/pdf"},
            key=key,
            expires_in=900,
            max_bytes=52428800,
        )

    def get_bytes(self, *, key: str) -> bytes:
        raise NotImplementedError


@pytest.fixture
def book_repo() -> FakeBookRepository:
    return FakeBookRepository()


@pytest.fixture
def chunk_repo() -> SpyChunkRepository:
    return SpyChunkRepository()


@pytest.fixture
def pdf_storage() -> FakePdfStorage:
    return FakePdfStorage()


# --- RequestBookUpload ---------------------------------------------------------


def test_request_book_upload_persists_book_with_source_key(book_repo, pdf_storage) -> None:
    use_case = RequestBookUpload(book_repo, pdf_storage, FixedClock(), SequentialIdGenerator())
    result = use_case.execute(RequestBookUploadCommand(user_id="user-1", title="My Book"))

    assert result.book.id == "id-1"
    assert result.book.source_key == "books/user-1/id-1/source.pdf"
    assert result.book.status.value == "UPLOADED"
    stored = book_repo.get("user-1", "id-1")
    assert stored == result.book


def test_request_book_upload_uses_injected_id_and_clock(book_repo, pdf_storage) -> None:
    use_case = RequestBookUpload(book_repo, pdf_storage, FixedClock(), SequentialIdGenerator())
    result = use_case.execute(RequestBookUploadCommand(user_id="user-1", title="My Book"))

    assert result.book.created_at == FIXED_NOW.isoformat()


def test_request_book_upload_returns_presigned_upload_for_the_same_key(
    book_repo, pdf_storage
) -> None:
    use_case = RequestBookUpload(book_repo, pdf_storage, FixedClock(), SequentialIdGenerator())
    result = use_case.execute(RequestBookUploadCommand(user_id="user-1", title="My Book"))

    assert result.upload.key == result.book.source_key
    assert pdf_storage.presigned_calls == [result.book.source_key]


def test_request_book_upload_rejects_blank_title(book_repo, pdf_storage) -> None:
    from src.shared_kernel.domain.errors import ValidationError

    use_case = RequestBookUpload(book_repo, pdf_storage, FixedClock(), SequentialIdGenerator())
    with pytest.raises(ValidationError):
        use_case.execute(RequestBookUploadCommand(user_id="user-1", title=""))
    # Nothing should have been saved or presigned for a rejected request.
    assert pdf_storage.presigned_calls == []


# --- ReissueBookUpload ----------------------------------------------------------


def _uploaded_book(**overrides) -> Book:
    return Book.create(
        id="book-1",
        user_id="user-1",
        title_raw="Title",
        now=FIXED_NOW,
        source_key="books/user-1/book-1/source.pdf",
        **overrides,
    )


def test_reissue_book_upload_for_uploaded_book_returns_presigned_upload(
    book_repo, pdf_storage
) -> None:
    book_repo.save(_uploaded_book())

    upload = ReissueBookUpload(book_repo, pdf_storage).execute("user-1", "book-1")

    assert upload.key == "books/user-1/book-1/source.pdf"
    assert pdf_storage.presigned_calls == ["books/user-1/book-1/source.pdf"]


def test_reissue_book_upload_for_failed_book_succeeds(book_repo, pdf_storage) -> None:
    book = _uploaded_book()
    book.status = BookStatus.FAILED
    book_repo.save(book)

    upload = ReissueBookUpload(book_repo, pdf_storage).execute("user-1", "book-1")
    assert upload.key == "books/user-1/book-1/source.pdf"


@pytest.mark.parametrize("status", [BookStatus.EXTRACTING, BookStatus.EXTRACTED, BookStatus.READY])
def test_reissue_book_upload_for_in_progress_book_raises_conflict(
    book_repo, pdf_storage, status: BookStatus
) -> None:
    book = _uploaded_book()
    book.status = status
    book_repo.save(book)

    with pytest.raises(ConflictError):
        ReissueBookUpload(book_repo, pdf_storage).execute("user-1", "book-1")
    assert pdf_storage.presigned_calls == []


def test_reissue_book_upload_for_another_users_book_raises_not_found(
    book_repo, pdf_storage
) -> None:
    book = Book.create(
        id="book-1", user_id="user-a", title_raw="Title", now=FIXED_NOW, source_key="books/user-a/book-1/source.pdf"
    )
    book_repo.save(book)

    with pytest.raises(NotFoundError):
        ReissueBookUpload(book_repo, pdf_storage).execute("user-b", "book-1")
    assert pdf_storage.presigned_calls == []


def test_reissue_book_upload_for_nonexistent_book_raises_not_found(book_repo, pdf_storage) -> None:
    with pytest.raises(NotFoundError):
        ReissueBookUpload(book_repo, pdf_storage).execute("user-1", "no-such-book")


# --- ListBooks -----------------------------------------------------------------


def test_list_books_returns_newest_first_for_caller_only(book_repo) -> None:
    older = Book.create(id="b-old", user_id="user-1", title_raw="Old", now=datetime(2026, 1, 1, tzinfo=UTC))
    newer = Book.create(id="b-new", user_id="user-1", title_raw="New", now=datetime(2026, 6, 1, tzinfo=UTC))
    other_user_book = Book.create(id="b-other", user_id="user-2", title_raw="Other", now=FIXED_NOW)
    book_repo.save(older)
    book_repo.save(newer)
    book_repo.save(other_user_book)

    result = ListBooks(book_repo).execute("user-1")

    assert [b.id for b in result] == ["b-new", "b-old"]


# --- GetBook -------------------------------------------------------------------


def test_get_book_for_another_users_book_raises_404_not_403(book_repo) -> None:
    book = Book.create(id="book-1", user_id="user-a", title_raw="Title", now=FIXED_NOW)
    book_repo.save(book)

    use_case = GetBook(book_repo)
    with pytest.raises(NotFoundError) as excinfo:
        use_case.execute("user-b", "book-1")

    assert excinfo.value.status_code == 404


def test_get_book_returns_callers_book(book_repo) -> None:
    book = Book.create(id="book-1", user_id="user-1", title_raw="Title", now=FIXED_NOW)
    book_repo.save(book)

    result = GetBook(book_repo).execute("user-1", "book-1")
    assert result == book


# --- ListBookChunks: the precise access-control test --------------------------


def test_list_book_chunks_for_another_users_book_never_calls_chunk_repo(
    book_repo, chunk_repo
) -> None:
    book = Book.create(id="book-1", user_id="user-a", title_raw="Title", now=FIXED_NOW)
    book_repo.save(book)

    use_case = ListBookChunks(book_repo, chunk_repo)
    with pytest.raises(NotFoundError):
        use_case.execute("user-b", "book-1")

    assert chunk_repo.calls == []


def test_list_book_chunks_for_owned_book_returns_chunks(book_repo, chunk_repo) -> None:
    book = Book.create(id="book-1", user_id="user-1", title_raw="Title", now=FIXED_NOW)
    book_repo.save(book)
    chunk = Chunk.create(book_id="book-1", user_id="user-1", index=0, text="hi", char_start=0, char_end=2)
    chunk_repo.save_all([chunk])

    result = ListBookChunks(book_repo, chunk_repo).execute("user-1", "book-1")
    assert result == [chunk]


# --- DeleteBook ----------------------------------------------------------------


def test_delete_book_deletes_chunks_then_book_in_order(book_repo, chunk_repo) -> None:
    book = Book.create(id="book-1", user_id="user-1", title_raw="Title", now=FIXED_NOW)
    book_repo.save(book)

    DeleteBook(book_repo, chunk_repo).execute("user-1", "book-1")

    assert chunk_repo.calls == [("delete_for_book", ("book-1",))]
    assert book_repo.get("user-1", "book-1") is None


def test_delete_book_for_another_users_book_raises_and_deletes_nothing(
    book_repo, chunk_repo
) -> None:
    book = Book.create(id="book-1", user_id="user-a", title_raw="Title", now=FIXED_NOW)
    book_repo.save(book)

    with pytest.raises(NotFoundError):
        DeleteBook(book_repo, chunk_repo).execute("user-b", "book-1")

    assert chunk_repo.calls == []
    assert book_repo.get("user-a", "book-1") is not None
