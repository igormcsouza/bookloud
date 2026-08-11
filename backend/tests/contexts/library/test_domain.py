from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.value_objects import (
    STITCHABLE_BOOK_STATUSES,
    TERMINAL_BOOK_STATUSES,
    BookStatus,
    ChunkStatus,
    StitchFailure,
    SynthesisSource,
)
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


def test_book_create_sets_source_key_when_provided() -> None:
    book = Book.create(
        id="book-1", user_id="user-1", title_raw="Title", now=FIXED_NOW, source_key="books/user-1/book-1/source.pdf"
    )
    assert book.source_key == "books/user-1/book-1/source.pdf"


def test_book_create_defaults_source_key_and_failure_reason_to_none() -> None:
    book = Book.create(id="book-1", user_id="user-1", title_raw="Title", now=FIXED_NOW)
    assert book.source_key is None
    assert book.failure_reason is None


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


def test_chunk_create_defaults_page_start_end_to_zero() -> None:
    chunk = Chunk.create(
        book_id="book-1", user_id="user-1", index=0, text="hello", char_start=0, char_end=5
    )
    assert chunk.page_start == 0
    assert chunk.page_end == 0


def test_chunk_create_sets_page_start_end_when_provided() -> None:
    chunk = Chunk.create(
        book_id="book-1",
        user_id="user-1",
        index=0,
        text="hello",
        char_start=0,
        char_end=5,
        page_start=2,
        page_end=3,
    )
    assert chunk.page_start == 2
    assert chunk.page_end == 3


def test_chunk_create_page_end_before_page_start_raises() -> None:
    with pytest.raises(ValidationError):
        Chunk.create(
            book_id="book-1",
            user_id="user-1",
            index=0,
            text="x",
            char_start=0,
            char_end=1,
            page_start=5,
            page_end=2,
        )


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


# --- phase 5 statuses & sets (PLANS/phase-5.md §5.2) ------------------------


@pytest.mark.parametrize("value", ["STITCHING", "PARTIAL"])
def test_book_status_parse_accepts_the_phase_5_values(value: str) -> None:
    assert BookStatus.parse(value).value == value


def test_stitchable_book_statuses_contents() -> None:
    """Deliberately includes STITCHING itself: a stitcher that died hard
    leaves the book there with no lease to expire, and excluding it would
    wedge the book permanently."""
    assert STITCHABLE_BOOK_STATUSES == (BookStatus.EXTRACTED, BookStatus.STITCHING)


def test_terminal_book_statuses_contents() -> None:
    assert TERMINAL_BOOK_STATUSES == (BookStatus.READY, BookStatus.PARTIAL, BookStatus.FAILED)


def test_stitching_is_not_terminal() -> None:
    assert BookStatus.STITCHING not in TERMINAL_BOOK_STATUSES


def test_stitch_failure_values() -> None:
    assert StitchFailure.NO_AUDIO.value == "NO_AUDIO"
    assert StitchFailure.STITCH_FAILED.value == "STITCH_FAILED"


def test_book_create_defaults_the_stitch_outputs() -> None:
    book = Book.create(id="book-1", user_id="user-1", title_raw="Title", now=FIXED_NOW)
    assert book.audio_key is None
    assert book.manifest_key is None
    assert book.audio_duration_ms == 0


def test_partial_is_not_claimable_or_reissuable() -> None:
    """PLANS/phase-5.md OQ-6 (DECIDED: leave both tuples unchanged). PARTIAL
    must stay out of both, or a stray redelivered S3 event could wipe and
    re-extract a book whose only problem was missing audio."""
    from src.contexts.library.application.extraction import _CLAIMABLE_STATUSES
    from src.contexts.library.application.use_cases import _REISSUABLE_STATUSES

    assert BookStatus.PARTIAL not in _CLAIMABLE_STATUSES
    assert BookStatus.PARTIAL not in _REISSUABLE_STATUSES
    assert BookStatus.STITCHING not in _CLAIMABLE_STATUSES
    assert BookStatus.STITCHING not in _REISSUABLE_STATUSES


def test_silent_is_a_recognized_synthesis_source() -> None:
    assert SynthesisSource.SILENT.value == "silent"
