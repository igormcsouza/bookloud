"""moto-mocked DynamoDB fixtures for the Library context's repository/use-case/
controller tests: ``dynamodb_table`` (a real, moto-backed DynamoDB API — not
a mocked boto3 call), ``book_repo``/``chunk_repo`` built against it,
``fixed_clock``/``seq_ids`` deterministic test doubles, and ``seed_book``/
``seed_chunks`` helpers that go through the repositories (per the phase
brief: Phase 2 fixtures create chunks via ``ChunkRepository.save_all``, never
through a real extraction pipeline).
"""

from __future__ import annotations

from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

import src.config as config
from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.infrastructure.dynamodb_book_repository import (
    DynamoDbBookRepository,
)
from src.contexts.library.infrastructure.dynamodb_chunk_repository import (
    DynamoDbChunkRepository,
)

FIXED_NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def dynamodb_table(monkeypatch: pytest.MonkeyPatch):
    """A real DynamoDB API (via moto's ``mock_aws()``) with the identical
    PK/SK key schema as ``infra/stacks/storage_stack.py`` and
    ``local/setup.sh`` -- keep these three in lockstep.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "test")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    # AWS_ENDPOINT_URL is a native boto3 env var (since botocore 1.31) -- if
    # a developer has it exported (or docker-compose leaks it), boto3 would
    # bypass moto entirely and hit LocalStack instead. Neutralize both the
    # env var and the settings singleton (built at import time, so a bare
    # setenv after import would do nothing -- see test_infrastructure_aws.py
    # for the same pattern).
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.setattr(config.settings, "aws_endpoint_url", "")
    monkeypatch.setattr(config.settings, "table_name", "bookloud-test")

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="bookloud-test",
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield resource.Table("bookloud-test")


@pytest.fixture
def book_repo(dynamodb_table) -> DynamoDbBookRepository:
    return DynamoDbBookRepository(table=dynamodb_table)


@pytest.fixture
def chunk_repo(dynamodb_table) -> DynamoDbChunkRepository:
    return DynamoDbChunkRepository(table=dynamodb_table)


class FixedClock:
    def __init__(self, now: datetime = FIXED_NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


@pytest.fixture
def fixed_clock() -> FixedClock:
    return FixedClock()


class SequentialIdGenerator:
    """Deterministic ``IdGenerator`` test double: ``id-1``, ``id-2``, ..."""

    def __init__(self) -> None:
        self._counter = 0

    def new_id(self) -> str:
        self._counter += 1
        return f"id-{self._counter}"


@pytest.fixture
def seq_ids() -> SequentialIdGenerator:
    return SequentialIdGenerator()


def seed_book(
    book_repo: DynamoDbBookRepository,
    *,
    id: str = "book-1",
    user_id: str = "user-1",
    title: str = "Seeded Book",
    now: datetime = FIXED_NOW,
) -> Book:
    """Insert a book straight through the repository and return the entity."""
    book = Book.create(id=id, user_id=user_id, title_raw=title, now=now)
    book_repo.save(book)
    return book


def seed_chunks(
    chunk_repo: DynamoDbChunkRepository,
    *,
    book_id: str = "book-1",
    user_id: str = "user-1",
    count: int = 3,
) -> list[Chunk]:
    """Create ``count`` chunks for ``book_id`` via ``save_all`` and return
    them -- this is the fixture-seeding path the phase brief specifies
    (chunks never go through a real extraction pipeline in Phase 2)."""
    chunks = [
        Chunk.create(
            book_id=book_id,
            user_id=user_id,
            index=i,
            text=f"chunk {i} text",
            char_start=i * 100,
            char_end=(i + 1) * 100,
        )
        for i in range(count)
    ]
    chunk_repo.save_all(chunks)
    return chunks
