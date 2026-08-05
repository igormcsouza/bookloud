from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.value_objects import BookStatus
from src.contexts.library.infrastructure.dynamodb_book_repository import (
    DynamoDbBookRepository,
)
from src.shared_kernel.domain.errors import ConflictError, NotFoundError
from tests.contexts.library.conftest import seed_book


def _client_error(code: str) -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": code, "Message": "boom"}},
        operation_name="UpdateItem",
    )


def _table_item_count(dynamodb_table) -> int:
    return dynamodb_table.scan()["Count"]


# 1. save -> get round-trips
def test_save_then_get_returns_equal_book(book_repo) -> None:
    book = seed_book(book_repo, id="book-1", user_id="user-1")
    fetched = book_repo.get("user-1", "book-1")
    assert fetched == book


# 2. Isolation: user A saves; get(user_b_sub, book_id) -> None
def test_isolation_get_by_wrong_user_returns_none(book_repo) -> None:
    seed_book(book_repo, id="book-1", user_id="user-a")
    assert book_repo.get("user-b", "book-1") is None


# 3. get of nonexistent id -> None
def test_get_nonexistent_book_returns_none(book_repo) -> None:
    assert book_repo.get("user-1", "no-such-book") is None


# 4. list_for_user empty -> []
def test_list_for_user_empty_library(book_repo) -> None:
    assert book_repo.list_for_user("user-1") == []


# 5. list_for_user returns only caller's books
def test_list_for_user_returns_only_callers_books(book_repo) -> None:
    seed_book(book_repo, id="book-1", user_id="user-a")
    seed_book(book_repo, id="book-2", user_id="user-b")
    books = book_repo.list_for_user("user-a")
    assert [b.id for b in books] == ["book-1"]


# 6. list_for_user newest-first by created_at
def test_list_for_user_newest_first(book_repo) -> None:
    from datetime import UTC, datetime

    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 6, 1, tzinfo=UTC)
    seed_book(book_repo, id="book-old", user_id="user-1", now=older)
    seed_book(book_repo, id="book-new", user_id="user-1", now=newer)
    books = book_repo.list_for_user("user-1")
    assert [b.id for b in books] == ["book-new", "book-old"]


# 7. list_for_user ignores a foreign-SK item under the same PK
def test_list_for_user_ignores_foreign_sk_item(book_repo, dynamodb_table) -> None:
    seed_book(book_repo, id="book-1", user_id="user-1")
    dynamodb_table.put_item(
        Item={"PK": "USER#user-1", "SK": "SOMETHING#not-a-book", "junk": "data"}
    )
    books = book_repo.list_for_user("user-1")
    assert [b.id for b in books] == ["book-1"]


# 8. save twice overwrites
def test_save_twice_overwrites(book_repo) -> None:
    book = seed_book(book_repo, id="book-1", user_id="user-1", title="Original")
    book.title = "Updated"
    book.status = BookStatus.EXTRACTED
    book_repo.save(book)
    fetched = book_repo.get("user-1", "book-1")
    assert fetched.title == "Updated"
    assert fetched.status == BookStatus.EXTRACTED


# 9. delete removes it; deleting nonexistent is a silent no-op
def test_delete_removes_book(book_repo) -> None:
    seed_book(book_repo, id="book-1", user_id="user-1")
    book_repo.delete("user-1", "book-1")
    assert book_repo.get("user-1", "book-1") is None


def test_delete_nonexistent_book_is_noop(book_repo) -> None:
    book_repo.delete("user-1", "no-such-book")  # must not raise


# 10. Isolation: delete(user_b, book_id) does not delete user A's book
def test_isolation_delete_by_wrong_user_does_not_delete(book_repo) -> None:
    seed_book(book_repo, id="book-1", user_id="user-a")
    book_repo.delete("user-b", "book-1")
    assert book_repo.get("user-a", "book-1") is not None


# 11. update_status round-trips, leaves title/chunksTotal untouched
def test_update_status_round_trips_and_preserves_other_fields(book_repo) -> None:
    book = seed_book(book_repo, id="book-1", user_id="user-1", title="Keep Me")
    book.chunks_total = 5
    book_repo.save(book)

    book_repo.update_status("user-1", "book-1", BookStatus.EXTRACTED)

    fetched = book_repo.get("user-1", "book-1")
    assert fetched.status == BookStatus.EXTRACTED
    assert fetched.title == "Keep Me"
    assert fetched.chunks_total == 5


# 12. update_status on nonexistent book -> NotFoundError, no item created
def test_update_status_nonexistent_book_raises_not_found_and_creates_nothing(
    book_repo, dynamodb_table
) -> None:
    before = _table_item_count(dynamodb_table)
    with pytest.raises(NotFoundError):
        book_repo.update_status("user-1", "no-such-book", BookStatus.EXTRACTED)
    assert _table_item_count(dynamodb_table) == before


# 13. increment_chunks_done returns 1 then 2; stored value matches
def test_increment_chunks_done_returns_incrementing_values(book_repo) -> None:
    seed_book(book_repo, id="book-1", user_id="user-1")
    assert book_repo.increment_chunks_done("user-1", "book-1") == 1
    assert book_repo.increment_chunks_done("user-1", "book-1") == 2
    fetched = book_repo.get("user-1", "book-1")
    assert fetched.chunks_done == 2


# 14. increment_chunks_done on nonexistent book -> NotFoundError, no item created
def test_increment_chunks_done_nonexistent_book_raises_not_found_and_creates_nothing(
    book_repo, dynamodb_table
) -> None:
    before = _table_item_count(dynamodb_table)
    with pytest.raises(NotFoundError):
        book_repo.increment_chunks_done("user-1", "no-such-book")
    assert _table_item_count(dynamodb_table) == before


def test_update_status_reraises_non_conditional_client_errors() -> None:
    stub_table = MagicMock()
    stub_table.update_item.side_effect = _client_error("ProvisionedThroughputExceededException")
    repo = DynamoDbBookRepository(table=stub_table)
    with pytest.raises(ClientError):
        repo.update_status("user-1", "book-1", BookStatus.EXTRACTED)


def test_increment_chunks_done_reraises_non_conditional_client_errors() -> None:
    stub_table = MagicMock()
    stub_table.update_item.side_effect = _client_error("ProvisionedThroughputExceededException")
    repo = DynamoDbBookRepository(table=stub_table)
    with pytest.raises(ClientError):
        repo.increment_chunks_done("user-1", "book-1")


# 15. Pagination: a stub table whose query returns LastEvaluatedKey once
def test_list_for_user_paginates() -> None:
    page_1_item = {
        "PK": "USER#user-1",
        "SK": "BOOK#book-1",
        "bookId": "book-1",
        "userId": "user-1",
        "title": "Page 1",
        "status": "UPLOADED",
        "chunksTotal": 0,
        "chunksDone": 0,
        "pageCount": 0,
        "createdAt": "2026-01-01T00:00:00+00:00",
        "updatedAt": "2026-01-01T00:00:00+00:00",
        "entityType": "BOOK",
    }
    page_2_item = {**page_1_item, "SK": "BOOK#book-2", "bookId": "book-2", "title": "Page 2"}

    stub_table = MagicMock()
    stub_table.query.side_effect = [
        {"Items": [page_1_item], "LastEvaluatedKey": {"PK": "USER#user-1", "SK": "BOOK#book-1"}},
        {"Items": [page_2_item]},
    ]

    repo = DynamoDbBookRepository(table=stub_table)
    books = repo.list_for_user("user-1")

    assert {b.id for b in books} == {"book-1", "book-2"}
    assert stub_table.query.call_count == 2
    # The second call must feed back the first page's LastEvaluatedKey.
    _, second_call_kwargs = stub_table.query.call_args_list[1]
    assert second_call_kwargs["ExclusiveStartKey"] == {
        "PK": "USER#user-1",
        "SK": "BOOK#book-1",
    }


# --- update_status: extraction kwargs (PLANS/phase-3.md §5.3) --------------


def test_update_status_sets_chunks_total_page_count_and_updated_at(book_repo) -> None:
    seed_book(book_repo, id="book-1", user_id="user-1")

    book_repo.update_status(
        "user-1",
        "book-1",
        BookStatus.EXTRACTED,
        chunks_total=7,
        page_count=42,
        updated_at="2026-08-05T00:00:00+00:00",
    )

    fetched = book_repo.get("user-1", "book-1")
    assert fetched.status == BookStatus.EXTRACTED
    assert fetched.chunks_total == 7
    assert fetched.page_count == 42
    assert fetched.updated_at == "2026-08-05T00:00:00+00:00"


def test_update_status_sets_failure_reason(book_repo) -> None:
    seed_book(book_repo, id="book-1", user_id="user-1")

    book_repo.update_status("user-1", "book-1", BookStatus.FAILED, failure_reason="CORRUPT_PDF")

    fetched = book_repo.get("user-1", "book-1")
    assert fetched.status == BookStatus.FAILED
    assert fetched.failure_reason == "CORRUPT_PDF"


def test_update_status_clear_failure_reason_removes_attribute(book_repo, dynamodb_table) -> None:
    seed_book(book_repo, id="book-1", user_id="user-1")
    book_repo.update_status("user-1", "book-1", BookStatus.FAILED, failure_reason="CORRUPT_PDF")

    book_repo.update_status("user-1", "book-1", BookStatus.EXTRACTED, clear_failure_reason=True)

    fetched = book_repo.get("user-1", "book-1")
    assert fetched.failure_reason is None
    # The attribute must be genuinely absent (REMOVE), not set to NULL --
    # book_mapper.py's convention is "absent, not null" for failureReason.
    raw_item = dynamodb_table.get_item(Key={"PK": "USER#user-1", "SK": "BOOK#book-1"})["Item"]
    assert "failureReason" not in raw_item


def test_update_status_failure_reason_and_clear_together_raises_value_error(book_repo) -> None:
    seed_book(book_repo, id="book-1", user_id="user-1")
    with pytest.raises(ValueError):
        book_repo.update_status(
            "user-1",
            "book-1",
            BookStatus.FAILED,
            failure_reason="CORRUPT_PDF",
            clear_failure_reason=True,
        )


def test_update_status_without_kwargs_still_works_like_phase_2(book_repo) -> None:
    seed_book(book_repo, id="book-1", user_id="user-1")
    book_repo.update_status("user-1", "book-1", BookStatus.EXTRACTED)
    assert book_repo.get("user-1", "book-1").status == BookStatus.EXTRACTED


# --- update_status: expected_statuses / ConflictError vs NotFoundError -----


def test_update_status_expected_statuses_succeeds_when_matching(book_repo) -> None:
    seed_book(book_repo, id="book-1", user_id="user-1")  # UPLOADED by default

    book_repo.update_status(
        "user-1",
        "book-1",
        BookStatus.EXTRACTING,
        expected_statuses=(BookStatus.UPLOADED, BookStatus.FAILED),
    )

    assert book_repo.get("user-1", "book-1").status == BookStatus.EXTRACTING


def test_update_status_expected_statuses_conflict_when_not_matching(book_repo) -> None:
    seed_book(book_repo, id="book-1", user_id="user-1")
    book_repo.update_status("user-1", "book-1", BookStatus.EXTRACTING)  # now EXTRACTING

    # A second concurrent claim attempt must see ConflictError, not
    # NotFoundError -- the book exists, it's just not in an expected state.
    with pytest.raises(ConflictError):
        book_repo.update_status(
            "user-1",
            "book-1",
            BookStatus.EXTRACTING,
            expected_statuses=(BookStatus.UPLOADED, BookStatus.FAILED),
        )
    # The claim attempt must not have mutated the book's status.
    assert book_repo.get("user-1", "book-1").status == BookStatus.EXTRACTING


def test_update_status_expected_statuses_not_found_when_book_missing(book_repo) -> None:
    with pytest.raises(NotFoundError):
        book_repo.update_status(
            "user-1",
            "no-such-book",
            BookStatus.EXTRACTING,
            expected_statuses=(BookStatus.UPLOADED, BookStatus.FAILED),
        )
