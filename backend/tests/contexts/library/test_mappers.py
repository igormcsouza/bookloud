from __future__ import annotations

from decimal import Decimal

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.value_objects import BookStatus, ChunkStatus
from src.contexts.library.infrastructure.book_mapper import book_to_item, item_to_book
from src.contexts.library.infrastructure.chunk_mapper import chunk_to_item, item_to_chunk
from src.contexts.library.infrastructure.keys import PK, SK, sk_chunk

# --- Book --------------------------------------------------------------------


def _book(**overrides) -> Book:
    defaults = dict(
        id="book-1",
        user_id="user-1",
        title="My Book",
        status=BookStatus.UPLOADED,
        chunks_total=0,
        chunks_done=0,
        page_count=0,
        created_at="2026-08-04T12:00:00+00:00",
        updated_at="2026-08-04T12:00:00+00:00",
    )
    defaults.update(overrides)
    return Book(**defaults)


def test_book_round_trip() -> None:
    book = _book()
    assert item_to_book(book_to_item(book)) == book


def test_book_to_item_sets_entity_type() -> None:
    item = book_to_item(_book())
    assert item["entityType"] == "BOOK"


def test_book_to_item_key_shape() -> None:
    item = book_to_item(_book(id="book-1", user_id="user-1"))
    assert item[PK] == "USER#user-1"
    assert item[SK] == "BOOK#book-1"


def test_item_to_book_coerces_decimal_to_int() -> None:
    item = book_to_item(_book())
    item["chunksTotal"] = Decimal("3")
    item["chunksDone"] = Decimal("1")
    item["pageCount"] = Decimal("42")
    book = item_to_book(item)
    assert book.chunks_total == 3
    assert isinstance(book.chunks_total, int)
    assert book.chunks_done == 1
    assert book.page_count == 42


def test_item_to_book_missing_optional_attrs_defaults() -> None:
    item = {
        PK: "USER#user-1",
        SK: "BOOK#book-1",
        "bookId": "book-1",
        "userId": "user-1",
    }
    book = item_to_book(item)
    assert book.title == ""
    assert book.status == BookStatus.UPLOADED
    assert book.chunks_total == 0
    assert book.chunks_done == 0
    assert book.page_count == 0
    assert book.created_at == ""
    assert book.updated_at == ""


def test_item_to_book_ignores_unknown_attrs() -> None:
    item = book_to_item(_book())
    item["someFutureAttribute"] = "unrelated"
    # Should not raise, and should still round-trip correctly.
    book = item_to_book(item)
    assert book.id == "book-1"


# --- Chunk -------------------------------------------------------------------


def _chunk(**overrides) -> Chunk:
    defaults = dict(
        book_id="book-1",
        index=0,
        user_id="user-1",
        text="hello",
        char_start=0,
        char_end=5,
        audio_key=None,
        marks_key=None,
        status=ChunkStatus.PENDING,
    )
    defaults.update(overrides)
    return Chunk(**defaults)


def test_chunk_round_trip() -> None:
    chunk = _chunk()
    assert item_to_chunk(chunk_to_item(chunk)) == chunk


def test_chunk_to_item_sets_entity_type() -> None:
    item = chunk_to_item(_chunk())
    assert item["entityType"] == "CHUNK"


def test_chunk_to_item_key_shape() -> None:
    item = chunk_to_item(_chunk(book_id="book-1", index=7))
    assert item[PK] == "BOOK#book-1"
    assert item[SK] == "CHUNK#000007"


def test_item_to_chunk_coerces_decimal_to_int() -> None:
    item = chunk_to_item(_chunk())
    item["charStart"] = Decimal("10")
    item["charEnd"] = Decimal("20")
    chunk = item_to_chunk(item)
    assert chunk.char_start == 10
    assert isinstance(chunk.char_start, int)
    assert chunk.char_end == 20


def test_item_to_chunk_missing_optional_attrs_defaults() -> None:
    item = {
        PK: "BOOK#book-1",
        SK: "CHUNK#000000",
        "bookId": "book-1",
        "chunkIndex": 0,
    }
    chunk = item_to_chunk(item)
    assert chunk.text == ""
    assert chunk.char_start == 0
    assert chunk.char_end == 0
    assert chunk.audio_key is None
    assert chunk.marks_key is None
    assert chunk.status == ChunkStatus.PENDING


def test_audio_marks_key_absent_maps_to_none() -> None:
    item = chunk_to_item(_chunk(audio_key="audio/key.mp3", marks_key="marks/key.json"))
    del item["audioKey"]
    del item["marksKey"]
    chunk = item_to_chunk(item)
    assert chunk.audio_key is None
    assert chunk.marks_key is None


def test_item_to_chunk_falls_back_to_sk_when_chunk_index_missing() -> None:
    item = {
        PK: "BOOK#book-1",
        SK: sk_chunk(9),
        "bookId": "book-1",
    }
    chunk = item_to_chunk(item)
    assert chunk.index == 9
