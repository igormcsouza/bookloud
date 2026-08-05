"""DynamoDB adapter for ``BookRepository``. Satisfies the Protocol
structurally -- no inheritance from it.

Uses the boto3 **resource** API (``Table``), not the low-level client, so no
``{"S": ...}`` marshalling appears anywhere -- ``boto3.dynamodb.conditions``'
``Key``/``Attr`` build the queries.
"""

from __future__ import annotations

from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.value_objects import BookStatus
from src.contexts.library.infrastructure.book_mapper import book_to_item, item_to_book
from src.contexts.library.infrastructure.keys import BOOK_PREFIX, PK, SK, pk_user, sk_book
from src.infrastructure.dynamodb import library_table
from src.shared_kernel.domain.errors import NotFoundError


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

    def update_status(self, user_id: str, book_id: str, status: BookStatus) -> None:
        try:
            self._table.update_item(
                Key={PK: pk_user(user_id), SK: sk_book(book_id)},
                # `status` is a DynamoDB reserved word -- must be aliased in
                # any UpdateExpression/ProjectionExpression/ConditionExpression.
                UpdateExpression="SET #status = :status",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":status": status.value},
                ConditionExpression="attribute_exists(PK)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise NotFoundError("Book not found") from exc
            raise

    def increment_chunks_done(self, user_id: str, book_id: str) -> int:
        try:
            response = self._table.update_item(
                Key={PK: pk_user(user_id), SK: sk_book(book_id)},
                UpdateExpression="ADD chunksDone :one",
                ExpressionAttributeValues={":one": 1},
                ConditionExpression="attribute_exists(PK)",
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise NotFoundError("Book not found") from exc
            raise
        return int(response["Attributes"]["chunksDone"])
