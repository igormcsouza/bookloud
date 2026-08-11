from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.value_objects import BookStatus, ChunkStatus
from src.contexts.library.infrastructure.book_mapper import book_to_item, item_to_book
from src.contexts.library.infrastructure.chunk_mapper import chunk_to_item, item_to_chunk
from src.contexts.library.infrastructure.keys import PK, SK, sk_chunk

FIXED_NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)

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


def test_book_to_item_writes_source_key() -> None:
    item = book_to_item(_book(source_key="books/user-1/book-1/source.pdf"))
    assert item["sourceKey"] == "books/user-1/book-1/source.pdf"


def test_book_to_item_omits_failure_reason_when_none() -> None:
    item = book_to_item(_book(failure_reason=None))
    assert "failureReason" not in item


def test_book_to_item_writes_failure_reason_when_set() -> None:
    item = book_to_item(_book(failure_reason="CORRUPT_PDF"))
    assert item["failureReason"] == "CORRUPT_PDF"


def test_item_to_book_source_key_and_failure_reason_absent_map_to_none() -> None:
    item = {
        PK: "USER#user-1",
        SK: "BOOK#book-1",
        "bookId": "book-1",
        "userId": "user-1",
    }
    book = item_to_book(item)
    assert book.source_key is None
    assert book.failure_reason is None


def test_item_to_book_ignores_unknown_attrs() -> None:
    item = book_to_item(_book())
    item["someFutureAttribute"] = "unrelated"
    # Should not raise, and should still round-trip correctly.
    book = item_to_book(item)
    assert book.id == "book-1"


# --- Book: chunksFailed (PLANS/phase-4.md §5.2) ------------------------------


def test_book_to_item_writes_chunks_failed() -> None:
    item = book_to_item(_book(chunks_failed=3))
    assert item["chunksFailed"] == 3


def test_item_to_book_coerces_decimal_chunks_failed() -> None:
    item = book_to_item(_book())
    item["chunksFailed"] = Decimal("2")
    book = item_to_book(item)
    assert book.chunks_failed == 2
    assert isinstance(book.chunks_failed, int)


def test_item_to_book_chunks_failed_defaults_to_zero_when_missing() -> None:
    item = {PK: "USER#user-1", SK: "BOOK#book-1", "bookId": "book-1", "userId": "user-1"}
    book = item_to_book(item)
    assert book.chunks_failed == 0


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


def test_chunk_to_item_writes_page_start_end() -> None:
    item = chunk_to_item(_chunk(page_start=3, page_end=5))
    assert item["pageStart"] == 3
    assert item["pageEnd"] == 5


def test_item_to_chunk_coerces_decimal_page_start_end() -> None:
    item = chunk_to_item(_chunk(page_start=3, page_end=5))
    item["pageStart"] = Decimal("3")
    item["pageEnd"] = Decimal("5")
    chunk = item_to_chunk(item)
    assert chunk.page_start == 3
    assert isinstance(chunk.page_start, int)
    assert chunk.page_end == 5


def test_item_to_chunk_page_start_end_default_to_zero_when_missing() -> None:
    item = {
        PK: "BOOK#book-1",
        SK: "CHUNK#000000",
        "bookId": "book-1",
        "chunkIndex": 0,
    }
    chunk = item_to_chunk(item)
    assert chunk.page_start == 0
    assert chunk.page_end == 0


def test_item_to_chunk_falls_back_to_sk_when_chunk_index_missing() -> None:
    item = {
        PK: "BOOK#book-1",
        SK: sk_chunk(9),
        "bookId": "book-1",
    }
    chunk = item_to_chunk(item)
    assert chunk.index == 9


# --- Chunk: durationMs / failureReason / synthesisSource (PLANS/phase-4.md §5.2) --


def test_chunk_to_item_writes_duration_ms() -> None:
    item = chunk_to_item(_chunk(duration_ms=118240))
    assert item["durationMs"] == 118240


def test_item_to_chunk_coerces_decimal_duration_ms() -> None:
    item = chunk_to_item(_chunk())
    item["durationMs"] = Decimal("5000")
    chunk = item_to_chunk(item)
    assert chunk.duration_ms == 5000
    assert isinstance(chunk.duration_ms, int)


def test_chunk_to_item_omits_failure_reason_and_synthesis_source_when_none() -> None:
    item = chunk_to_item(_chunk(failure_reason=None, synthesis_source=None))
    assert "failureReason" not in item
    assert "synthesisSource" not in item


def test_chunk_to_item_writes_failure_reason_and_synthesis_source_when_set() -> None:
    item = chunk_to_item(_chunk(failure_reason="ALL_ENGINES_FAILED", synthesis_source="edge-tts"))
    assert item["failureReason"] == "ALL_ENGINES_FAILED"
    assert item["synthesisSource"] == "edge-tts"


def test_item_to_chunk_duration_failure_reason_synthesis_source_default_when_missing() -> None:
    item = {PK: "BOOK#book-1", SK: "CHUNK#000000", "bookId": "book-1", "chunkIndex": 0}
    chunk = item_to_chunk(item)
    assert chunk.duration_ms == 0
    assert chunk.failure_reason is None
    assert chunk.synthesis_source is None


def test_chunk_round_trip_with_synthesis_fields() -> None:
    chunk = _chunk(duration_ms=5000, failure_reason="EMPTY_TEXT", synthesis_source="google-tts")
    assert item_to_chunk(chunk_to_item(chunk)) == chunk


# --- phase 5: stitch outputs round trip -------------------------------------


def test_book_to_item_writes_the_stitch_outputs_when_set() -> None:
    book = Book.create(id="book-1", user_id="user-1", title_raw="T", now=FIXED_NOW)
    book.audio_key = "audio/user-1/book-1/book.mp3"
    book.manifest_key = "marks/user-1/book-1/book.json"
    book.audio_duration_ms = 1418240

    item = book_to_item(book)

    assert item["audioKey"] == "audio/user-1/book-1/book.mp3"
    assert item["manifestKey"] == "marks/user-1/book-1/book.json"
    assert item["audioDurationMs"] == 1418240


def test_book_to_item_omits_absent_stitch_keys_rather_than_writing_null() -> None:
    """Absent (not NULL) when unset -- hence the adapter's REMOVE, matching
    failureReason's existing treatment."""
    item = book_to_item(Book.create(id="book-1", user_id="user-1", title_raw="T", now=FIXED_NOW))
    assert "audioKey" not in item
    assert "manifestKey" not in item
    assert item["audioDurationMs"] == 0


def test_item_to_book_round_trips_the_stitch_outputs() -> None:
    book = Book.create(id="book-1", user_id="user-1", title_raw="T", now=FIXED_NOW)
    book.status = BookStatus.PARTIAL
    book.audio_key = "audio/user-1/book-1/book.mp3"
    book.manifest_key = "marks/user-1/book-1/book.json"
    book.audio_duration_ms = 999

    restored = item_to_book(book_to_item(book))

    assert restored.audio_key == book.audio_key
    assert restored.manifest_key == book.manifest_key
    assert restored.audio_duration_ms == 999
    assert restored.status is BookStatus.PARTIAL


def test_item_to_book_defaults_absent_stitch_attributes() -> None:
    item = book_to_item(Book.create(id="book-1", user_id="user-1", title_raw="T", now=FIXED_NOW))
    del item["audioDurationMs"]

    restored = item_to_book(item)

    assert restored.audio_key is None
    assert restored.manifest_key is None
    assert restored.audio_duration_ms == 0


def test_item_to_book_coerces_the_decimal_audio_duration() -> None:
    item = book_to_item(Book.create(id="book-1", user_id="user-1", title_raw="T", now=FIXED_NOW))
    item["audioDurationMs"] = Decimal("1418240")

    restored = item_to_book(item)

    assert restored.audio_duration_ms == 1418240
    assert isinstance(restored.audio_duration_ms, int)
