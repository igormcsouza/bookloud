from __future__ import annotations

from datetime import UTC, datetime

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.storage import PresignedUpload
from src.contexts.library.interface.schemas import book_to_dict, chunk_to_dict, upload_to_dict

FIXED_NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


def test_book_to_dict_is_camel_case() -> None:
    book = Book.create(id="book-1", user_id="user-1", title_raw="Title", now=FIXED_NOW)
    result = book_to_dict(book)
    assert result == {
        "id": "book-1",
        "title": "Title",
        "status": "UPLOADED",
        "chunksTotal": 0,
        "chunksDone": 0,
        "pageCount": 0,
        "failureReason": None,
        "createdAt": FIXED_NOW.isoformat(),
        "updatedAt": FIXED_NOW.isoformat(),
    }


def test_book_to_dict_never_exposes_source_key() -> None:
    book = Book.create(
        id="book-1", user_id="user-1", title_raw="Title", now=FIXED_NOW, source_key="books/user-1/book-1/source.pdf"
    )
    result = book_to_dict(book)
    assert "sourceKey" not in result
    assert "source_key" not in result


def test_book_to_dict_surfaces_failure_reason_when_set() -> None:
    book = Book.create(id="book-1", user_id="user-1", title_raw="Title", now=FIXED_NOW)
    book.failure_reason = "CORRUPT_PDF"
    result = book_to_dict(book)
    assert result["failureReason"] == "CORRUPT_PDF"


def test_chunk_to_dict_is_camel_case() -> None:
    chunk = Chunk.create(
        book_id="book-1",
        user_id="user-1",
        index=3,
        text="hi",
        char_start=0,
        char_end=2,
        page_start=1,
        page_end=2,
    )
    result = chunk_to_dict(chunk)
    assert result == {
        "index": 3,
        "text": "hi",
        "charStart": 0,
        "charEnd": 2,
        "audioKey": None,
        "marksKey": None,
        "status": "PENDING",
        "pageStart": 1,
        "pageEnd": 2,
    }


def test_upload_to_dict_is_camel_case() -> None:
    upload = PresignedUpload(
        url="https://bucket.s3.amazonaws.com/",
        fields={"key": "books/user-1/book-1/source.pdf", "Content-Type": "application/pdf"},
        key="books/user-1/book-1/source.pdf",
        expires_in=900,
        max_bytes=52428800,
    )
    result = upload_to_dict(upload)
    assert result == {
        "url": "https://bucket.s3.amazonaws.com/",
        "fields": {"key": "books/user-1/book-1/source.pdf", "Content-Type": "application/pdf"},
        "key": "books/user-1/book-1/source.pdf",
        "expiresIn": 900,
        "maxBytes": 52428800,
    }
