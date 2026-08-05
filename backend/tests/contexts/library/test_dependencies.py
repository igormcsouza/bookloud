from __future__ import annotations

from src.contexts.library.infrastructure.dynamodb_book_repository import (
    DynamoDbBookRepository,
)
from src.contexts.library.infrastructure.dynamodb_chunk_repository import (
    DynamoDbChunkRepository,
)
from src.contexts.library.interface.dependencies import (
    get_book_repository,
    get_chunk_repository,
    get_clock,
    get_id_generator,
)
from src.infrastructure.clock import SystemClock
from src.infrastructure.ids import Uuid4IdGenerator


def test_get_book_repository_returns_dynamodb_adapter(dynamodb_table) -> None:
    assert isinstance(get_book_repository(), DynamoDbBookRepository)


def test_get_chunk_repository_returns_dynamodb_adapter(dynamodb_table) -> None:
    assert isinstance(get_chunk_repository(), DynamoDbChunkRepository)


def test_get_clock_returns_system_clock() -> None:
    assert isinstance(get_clock(), SystemClock)


def test_get_id_generator_returns_uuid4_generator() -> None:
    assert isinstance(get_id_generator(), Uuid4IdGenerator)
