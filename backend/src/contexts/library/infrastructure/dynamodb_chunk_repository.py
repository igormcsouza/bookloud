"""DynamoDB adapter for ``ChunkRepository``. Satisfies the Protocol
structurally -- no inheritance from it.

Performs **no access control** -- see ``domain/repository.py``'s module
docstring. Callers must authorize ``book_id`` via
``BookRepository.get(user_id, book_id)`` first.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.value_objects import ChunkStatus
from src.contexts.library.infrastructure.chunk_mapper import chunk_to_item, item_to_chunk
from src.contexts.library.infrastructure.keys import (
    CHUNK_PREFIX,
    PK,
    SK,
    pk_book,
    sk_chunk,
)
from src.infrastructure.dynamodb import library_table
from src.shared_kernel.domain.errors import NotFoundError


class DynamoDbChunkRepository:
    def __init__(self, table: Any | None = None) -> None:
        self._table = table if table is not None else library_table()

    def save(self, chunk: Chunk) -> None:
        self._table.put_item(Item=chunk_to_item(chunk))

    def save_all(self, chunks: Sequence[Chunk]) -> None:
        # batch_writer() handles the 25-item BatchWriteItem cap and
        # unprocessed-item retries for us.
        with self._table.batch_writer() as batch:
            for chunk in chunks:
                batch.put_item(Item=chunk_to_item(chunk))

    def get(self, book_id: str, index: int) -> Chunk | None:
        response = self._table.get_item(
            Key={PK: pk_book(book_id), SK: sk_chunk(index)}
        )
        item = response.get("Item")
        if item is None:
            return None
        return item_to_chunk(item)

    def list_for_book(self, book_id: str) -> list[Chunk]:
        items: list[dict] = []
        exclusive_start_key: dict | None = None
        while True:
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": Key(PK).eq(pk_book(book_id))
                & Key(SK).begins_with(CHUNK_PREFIX),
            }
            if exclusive_start_key is not None:
                kwargs["ExclusiveStartKey"] = exclusive_start_key
            response = self._table.query(**kwargs)
            items.extend(response.get("Items", []))
            exclusive_start_key = response.get("LastEvaluatedKey")
            if not exclusive_start_key:
                break

        # Ordering relies on the SK's ascending sort (DynamoDB default),
        # correct given zero-padding -- no Python sort needed.
        return [item_to_chunk(item) for item in items]

    def update_status(
        self,
        book_id: str,
        index: int,
        status: ChunkStatus,
        *,
        audio_key: str | None = None,
        marks_key: str | None = None,
    ) -> None:
        # `status` is a DynamoDB reserved word -- must be aliased in any
        # UpdateExpression/ProjectionExpression/ConditionExpression.
        set_clauses = ["#status = :status"]
        names = {"#status": "status"}
        values: dict[str, Any] = {":status": status.value}

        # Write audioKey/marksKey only when provided, so a status-only call
        # (phase 3) never nulls out keys a previous call (phase 4) set.
        if audio_key is not None:
            set_clauses.append("audioKey = :audioKey")
            values[":audioKey"] = audio_key
        if marks_key is not None:
            set_clauses.append("marksKey = :marksKey")
            values[":marksKey"] = marks_key

        try:
            self._table.update_item(
                Key={PK: pk_book(book_id), SK: sk_chunk(index)},
                UpdateExpression="SET " + ", ".join(set_clauses),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(PK)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise NotFoundError("Chunk not found") from exc
            raise

    def delete_for_book(self, book_id: str) -> int:
        # No cascade delete in DynamoDB: query the keys, then batch-delete.
        keys: list[dict] = []
        exclusive_start_key: dict | None = None
        while True:
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": Key(PK).eq(pk_book(book_id))
                & Key(SK).begins_with(CHUNK_PREFIX),
                "ProjectionExpression": "#pk, #sk",
                "ExpressionAttributeNames": {"#pk": PK, "#sk": SK},
            }
            if exclusive_start_key is not None:
                kwargs["ExclusiveStartKey"] = exclusive_start_key
            response = self._table.query(**kwargs)
            keys.extend(response.get("Items", []))
            exclusive_start_key = response.get("LastEvaluatedKey")
            if not exclusive_start_key:
                break

        with self._table.batch_writer() as batch:
            for key in keys:
                batch.delete_item(Key={PK: key[PK], SK: key[SK]})

        return len(keys)
