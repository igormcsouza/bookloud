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
from src.shared_kernel.domain.errors import ConflictError, NotFoundError


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
        expected_statuses: Sequence[ChunkStatus] | None = None,
        audio_key: str | None = None,
        marks_key: str | None = None,
        duration_ms: int | None = None,
        synthesis_source: str | None = None,
        failure_reason: str | None = None,
        clear_failure_reason: bool = False,
    ) -> None:
        if failure_reason is not None and clear_failure_reason:
            # Programming error, not a domain error.
            raise ValueError("failure_reason and clear_failure_reason are mutually exclusive")

        # `status` is a DynamoDB reserved word -- must be aliased in any
        # UpdateExpression/ProjectionExpression/ConditionExpression.
        set_clauses = ["#status = :status"]
        names: dict[str, str] = {"#status": "status"}
        values: dict[str, Any] = {":status": status.value}

        # Write audioKey/marksKey/... only when provided, so a status-only
        # call (phase 3) never nulls out keys a previous call (phase 4) set.
        if audio_key is not None:
            set_clauses.append("audioKey = :audioKey")
            values[":audioKey"] = audio_key
        if marks_key is not None:
            set_clauses.append("marksKey = :marksKey")
            values[":marksKey"] = marks_key
        if duration_ms is not None:
            set_clauses.append("durationMs = :durationMs")
            values[":durationMs"] = duration_ms
        if synthesis_source is not None:
            set_clauses.append("synthesisSource = :synthesisSource")
            values[":synthesisSource"] = synthesis_source
        if failure_reason is not None:
            set_clauses.append("failureReason = :failureReason")
            values[":failureReason"] = failure_reason

        update_expression = "SET " + ", ".join(set_clauses)
        if clear_failure_reason:
            # failureReason is absent (not NULL) when unset -- see
            # chunk_mapper.py -- so a successful DONE transition after a
            # prior FAILED attempt must REMOVE it, not SET it to None.
            update_expression += " REMOVE failureReason"

        condition_expression = "attribute_exists(PK)"
        if expected_statuses:
            expected_names = [f":exp{i}" for i in range(len(expected_statuses))]
            for name, expected in zip(expected_names, expected_statuses):
                values[name] = expected.value
            condition_expression += f" AND #status IN ({', '.join(expected_names)})"

        try:
            self._table.update_item(
                Key={PK: pk_book(book_id), SK: sk_chunk(index)},
                UpdateExpression=update_expression,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression=condition_expression,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Disambiguate "chunk is gone" from "chunk exists but failed
                # the expected_statuses condition" -- the single most
                # important line in phase 4 (§8.4's exactly-once counter
                # gate: a ConflictError here means another invocation
                # already reached the terminal state first, and this
                # invocation must NOT increment chunksDone again).
                if self.get(book_id, index) is None:
                    raise NotFoundError("Chunk not found") from exc
                raise ConflictError("Chunk is not in an expected status") from exc
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
