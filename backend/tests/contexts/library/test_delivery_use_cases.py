"""``GetBookAudioUrl``/``GetChunkAudioUrl``/``GetBookManifest``/
``GetChunkMarks`` (PLANS/phase-6.md §4.3, §13.2).

Two contracts carry this file:

1. **A foreign book is a 404, never a 403** -- the repo's standing rule since
   phase 2. Asserted separately for each of the four use cases, because the
   enforcement point (``_load_owned_book``) is a call each of them has to
   remember to make.
2. **"No audio" is a 409; "no such document" is a 404.** ``PARTIAL``/
   ``NO_AUDIO`` is a legitimate terminal state, not an error, so the client
   has to be able to tell it from a missing book.
"""

from __future__ import annotations

import json

import pytest

from src.contexts.library.application.delivery import (
    GetBookAudioUrl,
    GetBookManifest,
    GetChunkAudioUrl,
    GetChunkMarks,
)
from src.contexts.library.domain.value_objects import BookStatus, ChunkStatus
from src.shared_kernel.domain.errors import ConflictError, NotFoundError
from tests.contexts.library.conftest import seed_book, seed_chunks
from tests.contexts.library.fakes import FakeAudioDelivery, RecordingObjectStorage

USER = "user-1"
OTHER_USER = "user-2"
BOOK = "book-1"
AUDIO_KEY = f"audio/{USER}/{BOOK}/book.mp3"
MANIFEST_KEY = f"marks/{USER}/{BOOK}/book.json"
CHUNK_AUDIO_KEY = f"audio/{USER}/{BOOK}/000001.mp3"
CHUNK_MARKS_KEY = f"marks/{USER}/{BOOK}/000001.json"

MANIFEST_BYTES = json.dumps(
    {"version": 1, "bookId": BOOK, "durationMs": 4200, "segments": [], "missing": [0]},
    separators=(",", ":"),
).encode("utf-8")
MARKS_BYTES = json.dumps(
    {"version": 1, "bookId": BOOK, "chunkIndex": 1, "words": [{"t": 0, "d": 10, "s": 0, "e": 3, "w": "The"}]},
    separators=(",", ":"),
).encode("utf-8")


@pytest.fixture
def stitched_book(book_repo, dynamodb_table):
    seed_book(book_repo, id=BOOK, user_id=USER)
    book_repo.update_status(
        USER,
        BOOK,
        BookStatus.READY,
        audio_key=AUDIO_KEY,
        manifest_key=MANIFEST_KEY,
        audio_duration_ms=4200,
    )
    return book_repo.get(USER, BOOK)


@pytest.fixture
def marks_storage() -> RecordingObjectStorage:
    storage = RecordingObjectStorage()
    storage.objects[MANIFEST_KEY] = MANIFEST_BYTES
    storage.objects[CHUNK_MARKS_KEY] = MARKS_BYTES
    return storage


# --- GetBookAudioUrl -------------------------------------------------------


def test_get_book_audio_url_presigns_the_books_audio_key(book_repo, stitched_book) -> None:
    delivery = FakeAudioDelivery()

    result = GetBookAudioUrl(book_repo, delivery).execute(USER, BOOK)

    assert delivery.calls == [{"key": AUDIO_KEY, "expires_in": 3600}]
    assert "X-Amz-Signature" in result.download.url
    assert result.download.expires_in == 3600
    # From the DynamoDB row, not the file: for a mixed-engine book the
    # browser's own audio.duration is only an estimate.
    assert result.duration_ms == 4200
    assert result.content_type == "audio/mpeg"


def test_get_book_audio_url_without_audio_raises_conflict(book_repo, dynamodb_table) -> None:
    """The everyday state of every non-prod environment (phase-4 §0) -- a
    documented 409, deliberately distinguishable from a 404."""
    seed_book(book_repo, id=BOOK, user_id=USER)
    delivery = FakeAudioDelivery()

    with pytest.raises(ConflictError):
        GetBookAudioUrl(book_repo, delivery).execute(USER, BOOK)

    assert delivery.calls == []


def test_get_book_audio_url_for_another_users_book_is_not_found(book_repo, stitched_book) -> None:
    with pytest.raises(NotFoundError):
        GetBookAudioUrl(book_repo, FakeAudioDelivery()).execute(OTHER_USER, BOOK)


def test_get_book_audio_url_for_a_missing_book_is_not_found(book_repo, dynamodb_table) -> None:
    with pytest.raises(NotFoundError):
        GetBookAudioUrl(book_repo, FakeAudioDelivery()).execute(USER, "nope")


def test_get_book_audio_url_honours_expires_in(book_repo, stitched_book) -> None:
    delivery = FakeAudioDelivery()

    GetBookAudioUrl(book_repo, delivery).execute(USER, BOOK, expires_in=120)

    assert delivery.calls[0]["expires_in"] == 120


# --- GetChunkAudioUrl ------------------------------------------------------


@pytest.fixture
def synthesized_chunk(book_repo, chunk_repo, dynamodb_table):
    seed_book(book_repo, id=BOOK, user_id=USER)
    seed_chunks(chunk_repo, book_id=BOOK, user_id=USER, count=3)
    chunk_repo.update_status(
        BOOK,
        1,
        ChunkStatus.DONE,
        audio_key=CHUNK_AUDIO_KEY,
        marks_key=CHUNK_MARKS_KEY,
        duration_ms=1500,
    )


def test_get_chunk_audio_url_presigns_the_chunks_audio_key(
    book_repo, chunk_repo, synthesized_chunk
) -> None:
    delivery = FakeAudioDelivery()

    result = GetChunkAudioUrl(book_repo, chunk_repo, delivery).execute(USER, BOOK, 1)

    assert delivery.calls == [{"key": CHUNK_AUDIO_KEY, "expires_in": 3600}]
    assert result.duration_ms == 1500


def test_get_chunk_audio_url_without_audio_raises_conflict(
    book_repo, chunk_repo, dynamodb_table
) -> None:
    seed_book(book_repo, id=BOOK, user_id=USER)
    seed_chunks(chunk_repo, book_id=BOOK, user_id=USER, count=2)

    with pytest.raises(ConflictError):
        GetChunkAudioUrl(book_repo, chunk_repo, FakeAudioDelivery()).execute(USER, BOOK, 0)


def test_get_chunk_audio_url_for_a_missing_chunk_is_not_found(
    book_repo, chunk_repo, synthesized_chunk
) -> None:
    with pytest.raises(NotFoundError):
        GetChunkAudioUrl(book_repo, chunk_repo, FakeAudioDelivery()).execute(USER, BOOK, 99)


def test_get_chunk_audio_url_for_another_users_book_is_not_found(
    book_repo, chunk_repo, synthesized_chunk
) -> None:
    with pytest.raises(NotFoundError):
        GetChunkAudioUrl(book_repo, chunk_repo, FakeAudioDelivery()).execute(OTHER_USER, BOOK, 1)


# --- GetBookManifest -------------------------------------------------------


def test_get_book_manifest_returns_the_stored_bytes_verbatim(
    book_repo, stitched_book, marks_storage
) -> None:
    """Byte-for-byte, not re-serialized: the document is already the
    contract (phase-5 §7.2), and re-shaping it here would create a second
    place for the schema to drift."""
    payload = GetBookManifest(book_repo, marks_storage).execute(USER, BOOK)

    assert payload == MANIFEST_BYTES
    assert marks_storage.get_calls == [MANIFEST_KEY]


def test_get_book_manifest_without_a_manifest_key_is_not_found(
    book_repo, dynamodb_table, marks_storage
) -> None:
    seed_book(book_repo, id=BOOK, user_id=USER)

    with pytest.raises(NotFoundError):
        GetBookManifest(book_repo, marks_storage).execute(USER, BOOK)


def test_get_book_manifest_with_the_object_gone_is_not_found(
    book_repo, stitched_book, marks_storage
) -> None:
    """A PR bucket torn down under a still-open tab. §9's degradation table
    renders this as "text only, no audio", not a crash."""
    del marks_storage.objects[MANIFEST_KEY]

    with pytest.raises(NotFoundError):
        GetBookManifest(book_repo, marks_storage).execute(USER, BOOK)


def test_get_book_manifest_for_another_users_book_is_not_found(
    book_repo, stitched_book, marks_storage
) -> None:
    with pytest.raises(NotFoundError):
        GetBookManifest(book_repo, marks_storage).execute(OTHER_USER, BOOK)

    assert marks_storage.get_calls == []


# --- GetChunkMarks ---------------------------------------------------------


def test_get_chunk_marks_returns_the_stored_bytes_verbatim(
    book_repo, chunk_repo, synthesized_chunk, marks_storage
) -> None:
    payload = GetChunkMarks(book_repo, chunk_repo, marks_storage).execute(USER, BOOK, 1)

    assert payload == MARKS_BYTES


def test_get_chunk_marks_without_a_marks_key_is_not_found(
    book_repo, chunk_repo, synthesized_chunk, marks_storage
) -> None:
    """The normal case for every chunk in a PR environment -- which is
    exactly why the client negatively caches it."""
    with pytest.raises(NotFoundError):
        GetChunkMarks(book_repo, chunk_repo, marks_storage).execute(USER, BOOK, 0)


def test_get_chunk_marks_for_a_missing_chunk_is_not_found(
    book_repo, chunk_repo, synthesized_chunk, marks_storage
) -> None:
    with pytest.raises(NotFoundError):
        GetChunkMarks(book_repo, chunk_repo, marks_storage).execute(USER, BOOK, 99)


def test_get_chunk_marks_with_the_object_gone_is_not_found(
    book_repo, chunk_repo, synthesized_chunk, marks_storage
) -> None:
    del marks_storage.objects[CHUNK_MARKS_KEY]

    with pytest.raises(NotFoundError):
        GetChunkMarks(book_repo, chunk_repo, marks_storage).execute(USER, BOOK, 1)


def test_get_chunk_marks_for_another_users_book_is_not_found(
    book_repo, chunk_repo, synthesized_chunk, marks_storage
) -> None:
    with pytest.raises(NotFoundError):
        GetChunkMarks(book_repo, chunk_repo, marks_storage).execute(OTHER_USER, BOOK, 1)

    assert marks_storage.get_calls == []
