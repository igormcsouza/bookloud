from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.value_objects import ChunkStatus
from src.contexts.library.infrastructure.dynamodb_chunk_repository import (
    DynamoDbChunkRepository,
)
from src.shared_kernel.domain.errors import ConflictError, NotFoundError
from tests.contexts.library.conftest import seed_chunks


def _table_item_count(dynamodb_table) -> int:
    return dynamodb_table.scan()["Count"]


def _client_error(code: str) -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": code, "Message": "boom"}},
        operation_name="UpdateItem",
    )


# 1. save -> get round-trips, including unicode and multi-KB text
def test_save_then_get_round_trips_unicode_and_large_text(chunk_repo) -> None:
    big_text = ("Café — 日本語 — " * 500)  # a few KB, non-ASCII
    chunk = Chunk.create(
        book_id="book-1", user_id="user-1", index=0, text=big_text, char_start=0, char_end=len(big_text)
    )
    chunk_repo.save(chunk)
    fetched = chunk_repo.get("book-1", 0)
    assert fetched == chunk


# 2. save_all writes N; list_for_book returns them in index order (2 and 10)
def test_save_all_and_list_for_book_ordering_across_padding_boundary(chunk_repo) -> None:
    chunks = seed_chunks(chunk_repo, book_id="book-1", count=11)  # indices 0..10
    listed = chunk_repo.list_for_book("book-1")
    assert [c.index for c in listed] == list(range(11))
    assert chunks[2].index == 2
    assert chunks[10].index == 10


# 3. list_for_book on a book with no chunks -> []
def test_list_for_book_empty(chunk_repo) -> None:
    assert chunk_repo.list_for_book("no-such-book") == []


# 4. list_for_book for book A never returns book B's chunks
def test_list_for_book_scoped_to_book(chunk_repo) -> None:
    seed_chunks(chunk_repo, book_id="book-a", count=2)
    seed_chunks(chunk_repo, book_id="book-b", count=3)
    listed = chunk_repo.list_for_book("book-a")
    assert len(listed) == 2
    assert all(c.book_id == "book-a" for c in listed)


# 5. list_for_book ignores a foreign-SK item (phase-7 CHAT# guard)
def test_list_for_book_ignores_foreign_sk_item(chunk_repo, dynamodb_table) -> None:
    seed_chunks(chunk_repo, book_id="book-1", count=2)
    dynamodb_table.put_item(
        Item={"PK": "BOOK#book-1", "SK": "CHAT#msg-1", "role": "user", "content": "hi"}
    )
    listed = chunk_repo.list_for_book("book-1")
    assert len(listed) == 2
    assert all(c.index in (0, 1) for c in listed)


# 6. update_status with keys round-trips, leaves text/charStart/charEnd untouched
def test_update_status_with_keys_preserves_other_fields(chunk_repo) -> None:
    seed_chunks(chunk_repo, book_id="book-1", count=1)
    original = chunk_repo.get("book-1", 0)

    chunk_repo.update_status(
        "book-1", 0, ChunkStatus.DONE, audio_key="audio/0.mp3", marks_key="marks/0.json"
    )

    fetched = chunk_repo.get("book-1", 0)
    assert fetched.status == ChunkStatus.DONE
    assert fetched.audio_key == "audio/0.mp3"
    assert fetched.marks_key == "marks/0.json"
    assert fetched.text == original.text
    assert fetched.char_start == original.char_start
    assert fetched.char_end == original.char_end


# 7. update_status with no keys leaves existing audioKey/marksKey intact
def test_update_status_without_keys_preserves_existing_keys(chunk_repo) -> None:
    seed_chunks(chunk_repo, book_id="book-1", count=1)
    chunk_repo.update_status(
        "book-1", 0, ChunkStatus.DONE, audio_key="audio/0.mp3", marks_key="marks/0.json"
    )

    chunk_repo.update_status("book-1", 0, ChunkStatus.DONE)

    fetched = chunk_repo.get("book-1", 0)
    assert fetched.audio_key == "audio/0.mp3"
    assert fetched.marks_key == "marks/0.json"


# 8. update_status on nonexistent chunk -> NotFoundError, no item created
def test_update_status_nonexistent_chunk_raises_not_found_and_creates_nothing(
    chunk_repo, dynamodb_table
) -> None:
    before = _table_item_count(dynamodb_table)
    with pytest.raises(NotFoundError):
        chunk_repo.update_status("no-such-book", 0, ChunkStatus.DONE)
    assert _table_item_count(dynamodb_table) == before


# 9. delete_for_book removes all chunks and returns the count; second call returns 0
def test_delete_for_book_removes_all_and_returns_count(chunk_repo) -> None:
    seed_chunks(chunk_repo, book_id="book-1", count=4)
    deleted = chunk_repo.delete_for_book("book-1")
    assert deleted == 4
    assert chunk_repo.list_for_book("book-1") == []
    assert chunk_repo.delete_for_book("book-1") == 0


# 10. delete_for_book for book A leaves book B's chunks intact
def test_delete_for_book_scoped_to_book(chunk_repo) -> None:
    seed_chunks(chunk_repo, book_id="book-a", count=2)
    seed_chunks(chunk_repo, book_id="book-b", count=3)
    chunk_repo.delete_for_book("book-a")
    assert chunk_repo.list_for_book("book-a") == []
    assert len(chunk_repo.list_for_book("book-b")) == 3


def test_get_missing_chunk_returns_none(chunk_repo) -> None:
    assert chunk_repo.get("no-such-book", 0) is None


def test_update_status_reraises_non_conditional_client_errors() -> None:
    stub_table = MagicMock()
    stub_table.update_item.side_effect = _client_error("ProvisionedThroughputExceededException")
    repo = DynamoDbChunkRepository(table=stub_table)
    with pytest.raises(ClientError):
        repo.update_status("book-1", 0, ChunkStatus.DONE)


def test_list_for_book_paginates() -> None:
    page_1_item = {"PK": "BOOK#book-1", "SK": "CHUNK#000000", "bookId": "book-1", "chunkIndex": 0}
    page_2_item = {"PK": "BOOK#book-1", "SK": "CHUNK#000001", "bookId": "book-1", "chunkIndex": 1}

    stub_table = MagicMock()
    stub_table.query.side_effect = [
        {"Items": [page_1_item], "LastEvaluatedKey": {"PK": "BOOK#book-1", "SK": "CHUNK#000000"}},
        {"Items": [page_2_item]},
    ]

    repo = DynamoDbChunkRepository(table=stub_table)
    chunks = repo.list_for_book("book-1")

    assert [c.index for c in chunks] == [0, 1]
    assert stub_table.query.call_count == 2
    _, second_call_kwargs = stub_table.query.call_args_list[1]
    assert second_call_kwargs["ExclusiveStartKey"] == {"PK": "BOOK#book-1", "SK": "CHUNK#000000"}


# --- phase 4: expected_statuses / ConflictError vs NotFoundError -----------


def test_update_status_expected_statuses_succeeds_when_matching(chunk_repo) -> None:
    seed_chunks(chunk_repo, book_id="book-1", count=1)  # PENDING by default

    chunk_repo.update_status(
        "book-1",
        0,
        ChunkStatus.SYNTHESIZING,
        expected_statuses=(ChunkStatus.PENDING, ChunkStatus.SYNTHESIZING, ChunkStatus.FAILED),
    )

    assert chunk_repo.get("book-1", 0).status == ChunkStatus.SYNTHESIZING


def test_update_status_expected_statuses_conflict_when_already_done(chunk_repo) -> None:
    seed_chunks(chunk_repo, book_id="book-1", count=1)
    chunk_repo.update_status("book-1", 0, ChunkStatus.DONE)

    with pytest.raises(ConflictError):
        chunk_repo.update_status(
            "book-1",
            0,
            ChunkStatus.DONE,
            expected_statuses=(ChunkStatus.PENDING, ChunkStatus.SYNTHESIZING, ChunkStatus.FAILED),
        )
    # The conflicting attempt must not have mutated anything further.
    assert chunk_repo.get("book-1", 0).status == ChunkStatus.DONE


def test_update_status_expected_statuses_not_found_when_chunk_missing(chunk_repo) -> None:
    with pytest.raises(NotFoundError):
        chunk_repo.update_status(
            "no-such-book",
            0,
            ChunkStatus.SYNTHESIZING,
            expected_statuses=(ChunkStatus.PENDING,),
        )


# --- phase 4: duration_ms / synthesis_source / failure_reason --------------


def test_update_status_writes_duration_ms_and_synthesis_source(chunk_repo) -> None:
    seed_chunks(chunk_repo, book_id="book-1", count=1)

    chunk_repo.update_status(
        "book-1", 0, ChunkStatus.DONE, duration_ms=118240, synthesis_source="edge-tts"
    )

    fetched = chunk_repo.get("book-1", 0)
    assert fetched.duration_ms == 118240
    assert fetched.synthesis_source == "edge-tts"


def test_update_status_writes_failure_reason(chunk_repo) -> None:
    seed_chunks(chunk_repo, book_id="book-1", count=1)

    chunk_repo.update_status("book-1", 0, ChunkStatus.FAILED, failure_reason="ALL_ENGINES_FAILED")

    assert chunk_repo.get("book-1", 0).failure_reason == "ALL_ENGINES_FAILED"


def test_update_status_clear_failure_reason_removes_attribute(chunk_repo, dynamodb_table) -> None:
    seed_chunks(chunk_repo, book_id="book-1", count=1)
    chunk_repo.update_status("book-1", 0, ChunkStatus.FAILED, failure_reason="EMPTY_TEXT")

    chunk_repo.update_status("book-1", 0, ChunkStatus.DONE, clear_failure_reason=True)

    fetched = chunk_repo.get("book-1", 0)
    assert fetched.failure_reason is None
    raw_item = dynamodb_table.get_item(Key={"PK": "BOOK#book-1", "SK": "CHUNK#000000"})["Item"]
    assert "failureReason" not in raw_item


def test_update_status_failure_reason_and_clear_together_raises_value_error(chunk_repo) -> None:
    seed_chunks(chunk_repo, book_id="book-1", count=1)
    with pytest.raises(ValueError):
        chunk_repo.update_status(
            "book-1", 0, ChunkStatus.FAILED, failure_reason="EMPTY_TEXT", clear_failure_reason=True
        )


def test_update_status_only_call_does_not_null_out_audio_or_marks_key(chunk_repo) -> None:
    """A status-only call (e.g. the SYNTHESIZING claim) must never null out
    audioKey/marksKey a previous call already wrote."""
    seed_chunks(chunk_repo, book_id="book-1", count=1)
    chunk_repo.update_status("book-1", 0, ChunkStatus.DONE, audio_key="audio/0.mp3", marks_key="marks/0.json")

    chunk_repo.update_status(
        "book-1", 0, ChunkStatus.SYNTHESIZING, expected_statuses=(ChunkStatus.DONE,)
    )

    fetched = chunk_repo.get("book-1", 0)
    assert fetched.audio_key == "audio/0.mp3"
    assert fetched.marks_key == "marks/0.json"


def test_delete_for_book_paginates() -> None:
    page_1_key = {"PK": "BOOK#book-1", "SK": "CHUNK#000000"}
    page_2_key = {"PK": "BOOK#book-1", "SK": "CHUNK#000001"}

    stub_table = MagicMock()
    stub_table.query.side_effect = [
        {"Items": [page_1_key], "LastEvaluatedKey": {"PK": "BOOK#book-1", "SK": "CHUNK#000000"}},
        {"Items": [page_2_key]},
    ]
    batch_writer_cm = MagicMock()
    stub_table.batch_writer.return_value.__enter__.return_value = batch_writer_cm
    stub_table.batch_writer.return_value.__exit__.return_value = False

    repo = DynamoDbChunkRepository(table=stub_table)
    deleted = repo.delete_for_book("book-1")

    assert deleted == 2
    assert stub_table.query.call_count == 2
    assert batch_writer_cm.delete_item.call_count == 2
