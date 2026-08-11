from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.storage import PresignedUpload
from src.contexts.library.domain.value_objects import BookStatus
from src.contexts.library.interface.schemas import (
    book_status_to_dict,
    book_to_dict,
    chunk_to_dict,
    upload_to_dict,
)

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
        "chunksFailed": 0,
        "pageCount": 0,
        "failureReason": None,
        "audioKey": None,
        "manifestKey": None,
        "audioDurationMs": 0,
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
        "durationMs": 0,
        "failureReason": None,
        "synthesisSource": None,
    }


def test_book_to_dict_surfaces_chunks_failed() -> None:
    book = Book.create(id="book-1", user_id="user-1", title_raw="Title", now=FIXED_NOW)
    book.chunks_failed = 2
    result = book_to_dict(book)
    assert result["chunksFailed"] == 2


def test_chunk_to_dict_surfaces_synthesis_fields_when_set() -> None:
    chunk = Chunk.create(book_id="book-1", user_id="user-1", index=0, text="hi", char_start=0, char_end=2)
    chunk.duration_ms = 5000
    chunk.failure_reason = "ALL_ENGINES_FAILED"
    chunk.synthesis_source = "google-tts"
    result = chunk_to_dict(chunk)
    assert result["durationMs"] == 5000
    assert result["failureReason"] == "ALL_ENGINES_FAILED"
    assert result["synthesisSource"] == "google-tts"


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


# --- phase 5: stitch outputs + the status payload ---------------------------


def test_book_to_dict_surfaces_the_stitch_outputs() -> None:
    book = Book.create(id="book-1", user_id="user-1", title_raw="Title", now=FIXED_NOW)
    book.audio_key = "audio/user-1/book-1/book.mp3"
    book.manifest_key = "marks/user-1/book-1/book.json"
    book.audio_duration_ms = 1418240

    result = book_to_dict(book)

    assert result["audioKey"] == "audio/user-1/book-1/book.mp3"
    assert result["manifestKey"] == "marks/user-1/book-1/book.json"
    assert result["audioDurationMs"] == 1418240


def test_book_status_to_dict_has_the_documented_shape() -> None:
    book = Book.create(id="book-1", user_id="user-1", title_raw="Title", now=FIXED_NOW)
    book.status = BookStatus.PARTIAL
    book.chunks_total = 12
    book.chunks_done = 12
    book.chunks_failed = 12
    book.failure_reason = "NO_AUDIO"
    book.manifest_key = "marks/user-1/book-1/book.json"

    assert book_status_to_dict(book) == {
        "id": "book-1",
        "status": "PARTIAL",
        "terminal": True,
        "progress": {"chunksTotal": 12, "chunksDone": 12, "chunksFailed": 12, "percent": 100},
        "failureReason": "NO_AUDIO",
        "audio": {
            "audioKey": None,
            "manifestKey": "marks/user-1/book-1/book.json",
            "durationMs": 0,
        },
        "updatedAt": FIXED_NOW.isoformat(),
    }


@pytest.mark.parametrize(
    "status,terminal",
    [
        (BookStatus.UPLOADED, False),
        (BookStatus.EXTRACTING, False),
        (BookStatus.EXTRACTED, False),
        (BookStatus.STITCHING, False),
        (BookStatus.READY, True),
        (BookStatus.PARTIAL, True),
        (BookStatus.FAILED, True),
    ],
)
def test_terminal_flag_for_every_book_status(status: BookStatus, terminal: bool) -> None:
    """Every BookStatus value is covered, so adding a new one without
    deciding whether it is terminal fails here rather than hanging a poller."""
    book = Book.create(id="book-1", user_id="user-1", title_raw="T", now=FIXED_NOW)
    book.status = status
    assert book_status_to_dict(book)["terminal"] is terminal


def test_every_book_status_value_is_covered_by_the_terminal_table() -> None:
    covered = {BookStatus.UPLOADED, BookStatus.EXTRACTING, BookStatus.EXTRACTED,
               BookStatus.STITCHING, BookStatus.READY, BookStatus.PARTIAL, BookStatus.FAILED}
    assert covered == set(BookStatus)


@pytest.mark.parametrize(
    "done,total,status,expected",
    [
        (0, 4, BookStatus.EXTRACTED, 0),
        (1, 4, BookStatus.EXTRACTED, 25),
        (3, 4, BookStatus.EXTRACTED, 75),
        (4, 4, BookStatus.READY, 100),
        (0, 0, BookStatus.UPLOADED, 0),
        (0, 0, BookStatus.FAILED, 100),
    ],
)
def test_progress_percent(done: int, total: int, status: BookStatus, expected: int) -> None:
    book = Book.create(id="book-1", user_id="user-1", title_raw="T", now=FIXED_NOW)
    book.status = status
    book.chunks_total = total
    book.chunks_done = done
    assert book_status_to_dict(book)["progress"]["percent"] == expected
