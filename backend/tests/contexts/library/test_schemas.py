from __future__ import annotations

from datetime import UTC, datetime

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.interface.schemas import book_to_dict, chunk_to_dict

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
        "createdAt": FIXED_NOW.isoformat(),
        "updatedAt": FIXED_NOW.isoformat(),
    }


def test_chunk_to_dict_is_camel_case() -> None:
    chunk = Chunk.create(
        book_id="book-1", user_id="user-1", index=3, text="hi", char_start=0, char_end=2
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
    }
