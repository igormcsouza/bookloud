from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.value_objects import BookStatus, ChunkStatus
from src.shared_kernel.domain.errors import ValidationError

FIXED_NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


# --- Book.create -----------------------------------------------------------


def test_book_create_sets_uploaded_status_and_zero_counters() -> None:
    book = Book.create(id="book-1", user_id="user-1", title_raw="My Book", now=FIXED_NOW)

    assert book.status == BookStatus.UPLOADED
    assert book.chunks_total == 0
    assert book.chunks_done == 0
    assert book.page_count == 0


def test_book_create_uses_injected_id_and_clock() -> None:
    book = Book.create(id="book-42", user_id="user-1", title_raw="Title", now=FIXED_NOW)

    assert book.id == "book-42"
    assert book.user_id == "user-1"
    assert book.created_at == FIXED_NOW.isoformat()
    assert book.updated_at == FIXED_NOW.isoformat()


def test_book_create_strips_title() -> None:
    book = Book.create(id="book-1", user_id="user-1", title_raw="  Padded  ", now=FIXED_NOW)
    assert book.title == "Padded"


@pytest.mark.parametrize("bad_title", ["", "   ", None, 123])
def test_book_create_rejects_blank_or_non_str_title(bad_title: object) -> None:
    with pytest.raises(ValidationError):
        Book.create(id="book-1", user_id="user-1", title_raw=bad_title, now=FIXED_NOW)


@pytest.mark.parametrize("bad_user_id", ["", "   "])
def test_book_create_rejects_blank_user_id(bad_user_id: str) -> None:
    with pytest.raises(ValidationError):
        Book.create(id="book-1", user_id=bad_user_id, title_raw="Title", now=FIXED_NOW)


# --- Chunk.create ------------------------------------------------------------


def test_chunk_create_defaults_pending_with_no_keys() -> None:
    chunk = Chunk.create(
        book_id="book-1", user_id="user-1", index=0, text="hello", char_start=0, char_end=5
    )

    assert chunk.status == ChunkStatus.PENDING
    assert chunk.audio_key is None
    assert chunk.marks_key is None


def test_chunk_create_negative_index_raises() -> None:
    with pytest.raises(ValidationError):
        Chunk.create(
            book_id="book-1", user_id="user-1", index=-1, text="x", char_start=0, char_end=1
        )


def test_chunk_create_char_end_before_char_start_raises() -> None:
    with pytest.raises(ValidationError):
        Chunk.create(
            book_id="book-1", user_id="user-1", index=0, text="x", char_start=10, char_end=5
        )


def test_chunk_create_char_end_equal_char_start_is_ok() -> None:
    chunk = Chunk.create(
        book_id="book-1", user_id="user-1", index=0, text="", char_start=5, char_end=5
    )
    assert chunk.char_start == chunk.char_end == 5


# --- BookStatus / ChunkStatus parsing ----------------------------------------


@pytest.mark.parametrize("member", list(BookStatus))
def test_book_status_parse_accepts_every_member(member: BookStatus) -> None:
    assert BookStatus.parse(member.value) is member


@pytest.mark.parametrize("bad_value", ["NOPE", None, 123])
def test_book_status_parse_rejects_unknown(bad_value: object) -> None:
    with pytest.raises(ValidationError):
        BookStatus.parse(bad_value)


@pytest.mark.parametrize("member", list(ChunkStatus))
def test_chunk_status_parse_accepts_every_member(member: ChunkStatus) -> None:
    assert ChunkStatus.parse(member.value) is member


@pytest.mark.parametrize("bad_value", ["NOPE", None, 123])
def test_chunk_status_parse_rejects_unknown(bad_value: object) -> None:
    with pytest.raises(ValidationError):
        ChunkStatus.parse(bad_value)
