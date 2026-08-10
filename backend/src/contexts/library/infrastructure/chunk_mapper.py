"""dict (DynamoDB item shape) <-> ``Chunk`` domain entity conversion. See
``book_mapper.py``'s module docstring for the general conventions (camelCase
items, snake_case domain, ``item.get(name, default)`` for additive safety).
"""

from __future__ import annotations

from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.value_objects import ChunkStatus
from src.contexts.library.infrastructure.keys import (
    PK,
    SK,
    chunk_index_from_sk,
    pk_book,
    sk_chunk,
)


def chunk_to_item(chunk: Chunk) -> dict:
    item = {
        PK: pk_book(chunk.book_id),
        SK: sk_chunk(chunk.index),
        "entityType": "CHUNK",
        "bookId": chunk.book_id,
        "userId": chunk.user_id,
        "chunkIndex": chunk.index,
        "text": chunk.text,
        "charStart": chunk.char_start,
        "charEnd": chunk.char_end,
        # boto3 maps None -> NULL; never write "" for an absent key.
        "audioKey": chunk.audio_key,
        "marksKey": chunk.marks_key,
        "status": chunk.status.value,
        "pageStart": chunk.page_start,
        "pageEnd": chunk.page_end,
        "durationMs": chunk.duration_ms,
    }
    # failureReason/synthesisSource follow book_mapper.py's failureReason
    # convention: absent (not NULL) when unset, so a status-only update
    # never has to REMOVE something it never SET.
    if chunk.failure_reason is not None:
        item["failureReason"] = chunk.failure_reason
    if chunk.synthesis_source is not None:
        item["synthesisSource"] = chunk.synthesis_source
    return item


def item_to_chunk(item: dict) -> Chunk:
    # chunkIndex is stored explicitly *and* derivable from the SK; prefer
    # the attribute, fall back to parsing the SK (defensive: covers a
    # hand-seeded item missing it).
    index = (
        int(item["chunkIndex"])
        if "chunkIndex" in item
        else chunk_index_from_sk(item[SK])
    )
    return Chunk(
        book_id=item["bookId"],
        index=index,
        user_id=item.get("userId", ""),
        text=item.get("text", ""),
        # DynamoDB numbers always come back from boto3's resource API as
        # Decimal -- coerce to int.
        char_start=int(item.get("charStart", 0)),
        char_end=int(item.get("charEnd", 0)),
        audio_key=item.get("audioKey"),
        marks_key=item.get("marksKey"),
        status=ChunkStatus.parse(item.get("status", ChunkStatus.PENDING.value)),
        page_start=int(item.get("pageStart", 0)),
        page_end=int(item.get("pageEnd", 0)),
        duration_ms=int(item.get("durationMs", 0)),
        failure_reason=item.get("failureReason"),
        synthesis_source=item.get("synthesisSource"),
    )
