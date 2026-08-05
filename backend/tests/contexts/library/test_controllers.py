from __future__ import annotations

from datetime import UTC, datetime
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from tests.conftest import CLAIMS, WithGatewayClaims
from tests.contexts.library.conftest import seed_book


@pytest.fixture
def app_client(dynamodb_table) -> Iterator[TestClient]:
    """Plain (unauthenticated) client over the app, with the moto table
    active so `/books` doesn't 503 before the auth check even runs."""
    from src.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def authed_app_client(dynamodb_table) -> Iterator[TestClient]:
    """Authenticated client (simulating API Gateway's JWT authorizer having
    already run), with the moto table active. No dependency_overrides needed
    -- providers construct per request, and moto/monkeypatch are already
    active by the time the request handler runs."""
    from src.main import app

    with TestClient(WithGatewayClaims(app, CLAIMS)) as test_client:
        yield test_client


def _book_repo(dynamodb_table):
    from src.contexts.library.infrastructure.dynamodb_book_repository import (
        DynamoDbBookRepository,
    )

    return DynamoDbBookRepository(table=dynamodb_table)


def test_list_books_empty_table_returns_200_empty_array(authed_app_client) -> None:
    response = authed_app_client.get("/books")
    assert response.status_code == 200
    assert response.json() == []


def test_list_books_returns_camel_case_shape_newest_first(
    authed_app_client, dynamodb_table
) -> None:
    repo = _book_repo(dynamodb_table)
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 6, 1, tzinfo=UTC)
    seed_book(repo, id="book-old", user_id=CLAIMS["sub"], title="Old Book", now=older)
    seed_book(repo, id="book-new", user_id=CLAIMS["sub"], title="New Book", now=newer)

    response = authed_app_client.get("/books")
    assert response.status_code == 200
    body = response.json()
    assert [b["id"] for b in body] == ["book-new", "book-old"]
    (book,) = [b for b in body if b["id"] == "book-new"]
    assert book == {
        "id": "book-new",
        "title": "New Book",
        "status": "UPLOADED",
        "chunksTotal": 0,
        "chunksDone": 0,
        "pageCount": 0,
        "createdAt": newer.isoformat(),
        "updatedAt": newer.isoformat(),
    }


def test_list_books_anonymous_returns_401(app_client) -> None:
    response = app_client.get("/books")
    assert response.status_code == 401


def test_get_book_for_callers_book_returns_200(authed_app_client, dynamodb_table) -> None:
    repo = _book_repo(dynamodb_table)
    seed_book(repo, id="book-1", user_id=CLAIMS["sub"], title="Mine")

    response = authed_app_client.get("/books/book-1")
    assert response.status_code == 200
    assert response.json()["title"] == "Mine"


def test_get_book_for_another_users_book_returns_404(authed_app_client, dynamodb_table) -> None:
    repo = _book_repo(dynamodb_table)
    seed_book(repo, id="book-1", user_id="someone-else", title="Not Mine")

    response = authed_app_client.get("/books/book-1")
    assert response.status_code == 404


def test_get_book_for_nonexistent_id_returns_404(authed_app_client) -> None:
    response = authed_app_client.get("/books/no-such-book")
    assert response.status_code == 404
