"""dict (DynamoDB item shape) <-> ``Book`` domain entity conversion.

Attribute names follow ``IMPLEMENTATION_PLAN.md``'s data model verbatim
(camelCase); Python domain fields stay snake_case — this module is exactly
what bridges the two.

``item_to_book`` uses ``item.get(name, default)`` for everything except the
identity fields (``PK``/``SK``/``bookId``), mirroring jgautocar's
``car_mapper.py``. This is what makes a later phase's new attribute (e.g.
phase 3's ``sourceKey``) a purely additive one-line change here with no
migration.
"""

from __future__ import annotations

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.value_objects import BookStatus
from src.contexts.library.infrastructure.keys import PK, SK, pk_user, sk_book


def book_to_item(book: Book) -> dict:
    item = {
        PK: pk_user(book.user_id),
        SK: sk_book(book.id),
        "entityType": "BOOK",
        "bookId": book.id,
        "userId": book.user_id,
        "title": book.title,
        "status": book.status.value,
        "chunksTotal": book.chunks_total,
        "chunksDone": book.chunks_done,
        "pageCount": book.page_count,
        "createdAt": book.created_at,
        "updatedAt": book.updated_at,
        # boto3 maps None -> NULL; write it explicitly rather than "" so
        # item_to_book's `.get(...)` round-trips a real None.
        "sourceKey": book.source_key,
    }
    if book.failure_reason is not None:
        item["failureReason"] = book.failure_reason
    return item


def item_to_book(item: dict) -> Book:
    return Book(
        id=item["bookId"],
        user_id=item["userId"],
        title=item.get("title", ""),
        status=BookStatus.parse(item.get("status", BookStatus.UPLOADED.value)),
        # DynamoDB numbers always come back from boto3's resource API as
        # Decimal -- coerce to int.
        chunks_total=int(item.get("chunksTotal", 0)),
        chunks_done=int(item.get("chunksDone", 0)),
        page_count=int(item.get("pageCount", 0)),
        created_at=item.get("createdAt", ""),
        updated_at=item.get("updatedAt", ""),
        source_key=item.get("sourceKey"),
        failure_reason=item.get("failureReason"),
    )
