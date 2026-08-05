"""The ``Book`` aggregate root.

Unlike jgautocar's ``Car`` (which embeds ``tasks``/``photos`` as entities
nested inside the same DynamoDB item, with no independent repository for
either), ``Book`` and ``Chunk`` are deliberately **separate aggregates**,
each with its own repository. Three reasons, in increasing order of
importance:

1. The single-table schema in ``IMPLEMENTATION_PLAN.md`` already puts chunks
   in their own items (``PK=BOOK#<bookId>`` / ``SK=CHUNK#<n>``), not nested
   in the book row.
2. A whole book's chunk ``text`` would blow past DynamoDB's 400 KB per-item
   limit if embedded.
3. **The decisive reason:** ``IMPLEMENTATION_PLAN.md``'s key architecture
   decisions specify that chunk synthesis (phase 4) runs in parallel — one
   SQS-triggered Lambda invocation per chunk — with fan-in via an *atomic*
   ``chunksDone`` counter on the book record. Read-modify-write of one
   embedded aggregate from N concurrent Lambda invocations would lose
   updates. Separate items updated via targeted ``UpdateItem`` calls is the
   whole point of this schema.

Consequence, stated explicitly here so no later phase "fixes" it by
recomputing: **``chunks_done``/``chunks_total`` are eventually consistent
with the actual chunk items by design.** They are counters maintained by
``BookRepository.increment_chunks_done``/targeted updates, not a derived
invariant — no code should ever recompute them by listing chunks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.contexts.library.domain.value_objects import BookStatus
from src.shared_kernel.domain.errors import ValidationError


@dataclass
class Book:
    id: str
    user_id: str  # Cognito `sub` (phase-1 §1.2) -- never the username
    title: str
    status: BookStatus
    chunks_total: int
    chunks_done: int
    page_count: int
    created_at: str
    updated_at: str
    source_key: str | None = None  # S3 key in pdf_bucket; set at creation (phase 3)
    failure_reason: str | None = None  # ExtractionFailure value; None unless FAILED

    @classmethod
    def create(
        cls,
        *,
        id: str,
        user_id: str,
        title_raw: object,
        now: datetime,
        source_key: str | None = None,
    ) -> Book:
        if not isinstance(title_raw, str) or not title_raw.strip():
            raise ValidationError("Title is required")
        if not user_id or not user_id.strip():
            # Defence in depth: an empty partition key would silently create
            # a cross-tenant bucket (PK="USER#") that every user's books
            # would land in were a caller ever to slip through the auth
            # dependency with a blank sub.
            raise ValidationError("User id is required")

        timestamp = now.isoformat()
        return cls(
            id=id,
            user_id=user_id,
            title=title_raw.strip(),
            status=BookStatus.UPLOADED,
            chunks_total=0,
            chunks_done=0,
            page_count=0,
            created_at=timestamp,
            updated_at=timestamp,
            source_key=source_key,
            failure_reason=None,
        )
