"""DynamoDB adapter for ``BookRepository``. Satisfies the Protocol
structurally -- no inheritance from it.

Uses the boto3 **resource** API (``Table``), not the low-level client, so no
``{"S": ...}`` marshalling appears anywhere -- ``boto3.dynamodb.conditions``'
``Key``/``Attr`` build the queries.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.repository import ChunkCounters
from src.contexts.library.domain.value_objects import BookStatus
from src.contexts.library.infrastructure.book_mapper import book_to_item, item_to_book
from src.contexts.library.infrastructure.keys import BOOK_PREFIX, PK, SK, pk_user, sk_book
from src.infrastructure.dynamodb import library_table
from src.shared_kernel.domain.errors import ConflictError, NotFoundError


class DynamoDbBookRepository:
    def __init__(self, table: Any | None = None) -> None:
        self._table = table if table is not None else library_table()

    def save(self, book: Book) -> None:
        self._table.put_item(Item=book_to_item(book))

    def get(self, user_id: str, book_id: str) -> Book | None:
        response = self._table.get_item(
            Key={PK: pk_user(user_id), SK: sk_book(book_id)}
        )
        item = response.get("Item")
        if item is None:
            return None
        return item_to_book(item)

    def list_for_user(self, user_id: str) -> list[Book]:
        items: list[dict] = []
        exclusive_start_key: dict | None = None
        while True:
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": Key(PK).eq(pk_user(user_id))
                & Key(SK).begins_with(BOOK_PREFIX),
            }
            if exclusive_start_key is not None:
                kwargs["ExclusiveStartKey"] = exclusive_start_key
            response = self._table.query(**kwargs)
            items.extend(response.get("Items", []))
            exclusive_start_key = response.get("LastEvaluatedKey")
            if not exclusive_start_key:
                break

        books = [item_to_book(item) for item in items]
        # Query returns books ordered by UUID (meaningless); sort newest
        # first by created_at for the phase-6 sidebar. A GSI/timestamp-SK
        # would both be over-engineering for a personal library of tens of
        # books -- see PLANS/phase-2.md §3.2.
        books.sort(key=lambda b: b.created_at, reverse=True)
        return books

    def delete(self, user_id: str, book_id: str) -> None:
        self._table.delete_item(Key={PK: pk_user(user_id), SK: sk_book(book_id)})

    def update_status(
        self,
        user_id: str,
        book_id: str,
        status: BookStatus,
        *,
        expected_statuses: Sequence[BookStatus] | None = None,
        chunks_total: int | None = None,
        chunks_done: int | None = None,
        chunks_failed: int | None = None,
        page_count: int | None = None,
        failure_reason: str | None = None,
        clear_failure_reason: bool = False,
        updated_at: str | None = None,
    ) -> None:
        if failure_reason is not None and clear_failure_reason:
            # Programming error, not a domain error -- the caller asked to
            # both set and clear the same attribute in one call.
            raise ValueError("failure_reason and clear_failure_reason are mutually exclusive")

        # `status` is a DynamoDB reserved word -- must be aliased in any
        # UpdateExpression/ProjectionExpression/ConditionExpression.
        set_clauses = ["#status = :status"]
        names: dict[str, str] = {"#status": "status"}
        values: dict[str, Any] = {":status": status.value}

        if chunks_total is not None:
            set_clauses.append("chunksTotal = :chunksTotal")
            values[":chunksTotal"] = chunks_total
        if chunks_done is not None:
            # PLANS/phase-4.md §5.4: the EXTRACTED flip resets both counters
            # to 0 so re-extracting a previously-FAILED book doesn't leave a
            # stale chunksDone/chunksFailed behind.
            set_clauses.append("chunksDone = :chunksDone")
            values[":chunksDone"] = chunks_done
        if chunks_failed is not None:
            set_clauses.append("chunksFailed = :chunksFailed")
            values[":chunksFailed"] = chunks_failed
        if page_count is not None:
            set_clauses.append("pageCount = :pageCount")
            values[":pageCount"] = page_count
        if failure_reason is not None:
            set_clauses.append("failureReason = :failureReason")
            values[":failureReason"] = failure_reason
        if updated_at is not None:
            set_clauses.append("updatedAt = :updatedAt")
            values[":updatedAt"] = updated_at

        update_expression = "SET " + ", ".join(set_clauses)
        if clear_failure_reason:
            # A single UpdateExpression may combine SET and REMOVE.
            # failureReason is absent (not NULL) when unset -- see
            # book_mapper.py -- so retrying a previously FAILED book must
            # REMOVE it, not SET it to None.
            update_expression += " REMOVE failureReason"

        condition_expression = "attribute_exists(PK)"
        if expected_statuses:
            expected_names = [f":exp{i}" for i in range(len(expected_statuses))]
            for name, expected in zip(expected_names, expected_statuses):
                values[name] = expected.value
            condition_expression += f" AND #status IN ({', '.join(expected_names)})"

        try:
            self._table.update_item(
                Key={PK: pk_user(user_id), SK: sk_book(book_id)},
                UpdateExpression=update_expression,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression=condition_expression,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Disambiguate "book is gone" from "book exists but failed
                # the expected_statuses condition" -- without this the
                # atomic EXTRACTING claim (PLANS/phase-3.md §8.2) can't tell
                # a genuinely missing book from one already claimed by
                # another concurrent invocation.
                if self.get(user_id, book_id) is None:
                    raise NotFoundError("Book not found") from exc
                raise ConflictError("Book is not in an expected status") from exc
            raise

    def increment_chunks_done(
        self, user_id: str, book_id: str, *, failed: bool = False
    ) -> ChunkCounters:
        update_expression = "ADD chunksDone :one"
        if failed:
            update_expression += ", chunksFailed :one"
        try:
            response = self._table.update_item(
                Key={PK: pk_user(user_id), SK: sk_book(book_id)},
                UpdateExpression=update_expression,
                ExpressionAttributeValues={":one": 1},
                ConditionExpression="attribute_exists(PK)",
                # ALL_NEW (not UPDATED_NEW): the caller needs chunksTotal too
                # (to compute is_complete), and ADD only ever touches
                # chunksDone/chunksFailed -- UPDATED_NEW would omit
                # chunksTotal entirely since this call never sets it.
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise NotFoundError("Book not found") from exc
            raise
        attrs = response["Attributes"]
        # boto3 resource API returns Decimal for numbers -- coerce to int.
        return ChunkCounters(
            chunks_done=int(attrs.get("chunksDone", 0)),
            chunks_total=int(attrs.get("chunksTotal", 0)),
            chunks_failed=int(attrs.get("chunksFailed", 0)),
        )
