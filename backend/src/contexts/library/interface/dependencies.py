"""FastAPI ``Depends`` providers -- the composition root for the Library
context. Each provider constructs **per request**, never at import time: the
FastAPI equivalent of jgautocar's DI container resolved fresh on every
request. This is also what lets the moto fixture work with zero
``app.dependency_overrides`` -- ``DynamoDbBookRepository()``/
``DynamoDbChunkRepository()`` call ``library_table()`` at call time, by
which point a test's ``mock_aws()``/``monkeypatch`` is already active.
"""

from __future__ import annotations

from src.contexts.library.domain.repository import BookRepository, ChunkRepository
from src.contexts.library.infrastructure.dynamodb_book_repository import (
    DynamoDbBookRepository,
)
from src.contexts.library.infrastructure.dynamodb_chunk_repository import (
    DynamoDbChunkRepository,
)
from src.infrastructure.clock import SystemClock
from src.infrastructure.ids import Uuid4IdGenerator
from src.shared_kernel.application.ports import Clock, IdGenerator


def get_book_repository() -> BookRepository:
    return DynamoDbBookRepository()


def get_chunk_repository() -> ChunkRepository:
    return DynamoDbChunkRepository()


def get_clock() -> Clock:
    return SystemClock()


def get_id_generator() -> IdGenerator:
    return Uuid4IdGenerator()
