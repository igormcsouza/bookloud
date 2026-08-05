from __future__ import annotations

from datetime import UTC, datetime
from typing import Iterator

import boto3
import pytest
from fastapi.testclient import TestClient

from tests.conftest import CLAIMS, WithGatewayClaims
from tests.contexts.library.conftest import seed_book, seed_chunks

PDF_BUCKET = "bookloud-test-pdfs"


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


@pytest.fixture
def pdf_bucket(dynamodb_table, monkeypatch: pytest.MonkeyPatch):
    """The presigned-upload routes (POST /books, POST /books/{id}/upload-url)
    build an ``S3PdfStorage(bucket=settings.pdf_bucket)`` per request -- this
    points that at a real (moto) S3 bucket, the same pattern as
    ``dynamodb_table`` for DynamoDB."""
    import src.config as config

    monkeypatch.setattr(config.settings, "pdf_bucket", PDF_BUCKET)
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=PDF_BUCKET)
    return client


def _book_repo(dynamodb_table):
    from src.contexts.library.infrastructure.dynamodb_book_repository import (
        DynamoDbBookRepository,
    )

    return DynamoDbBookRepository(table=dynamodb_table)


# --- POST /books ---------------------------------------------------------------


def test_create_book_returns_201_with_book_and_upload(authed_app_client, pdf_bucket) -> None:
    response = authed_app_client.post("/books", json={"title": "My New Book"})
    assert response.status_code == 201
    body = response.json()

    assert body["book"]["title"] == "My New Book"
    assert body["book"]["status"] == "UPLOADED"
    assert body["book"]["failureReason"] is None
    assert "sourceKey" not in body["book"]

    upload = body["upload"]
    assert upload["key"].startswith(f"books/{CLAIMS['sub']}/")
    assert upload["key"].endswith("/source.pdf")
    assert upload["fields"]["key"] == upload["key"]
    assert upload["expiresIn"] == 900
    assert upload["maxBytes"] == 52428800


def test_create_book_persists_the_book(authed_app_client, pdf_bucket, dynamodb_table) -> None:
    response = authed_app_client.post("/books", json={"title": "Persisted Book"})
    book_id = response.json()["book"]["id"]

    repo = _book_repo(dynamodb_table)
    stored = repo.get(CLAIMS["sub"], book_id)
    assert stored is not None
    assert stored.title == "Persisted Book"
    assert stored.source_key == f"books/{CLAIMS['sub']}/{book_id}/source.pdf"


def test_create_book_blank_title_returns_400(authed_app_client, pdf_bucket) -> None:
    response = authed_app_client.post("/books", json={"title": "   "})
    assert response.status_code == 400


def test_create_book_anonymous_returns_401(app_client, pdf_bucket) -> None:
    response = app_client.post("/books", json={"title": "Nope"})
    assert response.status_code == 401


# --- POST /books/{id}/upload-url ------------------------------------------------


def test_reissue_upload_url_for_uploaded_book_returns_200(
    authed_app_client, pdf_bucket, dynamodb_table
) -> None:
    repo = _book_repo(dynamodb_table)
    from src.contexts.library.domain.book import Book

    book = Book.create(
        id="book-1",
        user_id=CLAIMS["sub"],
        title_raw="Mine",
        now=datetime.now(UTC),
        source_key="books/" + CLAIMS["sub"] + "/book-1/source.pdf",
    )
    repo.save(book)

    response = authed_app_client.post("/books/book-1/upload-url")
    assert response.status_code == 200
    body = response.json()
    assert body["key"] == book.source_key


def test_reissue_upload_url_for_extracting_book_returns_409(
    authed_app_client, pdf_bucket, dynamodb_table
) -> None:
    from src.contexts.library.domain.book import Book
    from src.contexts.library.domain.value_objects import BookStatus

    repo = _book_repo(dynamodb_table)
    book = Book.create(
        id="book-1",
        user_id=CLAIMS["sub"],
        title_raw="Mine",
        now=datetime.now(UTC),
        source_key="books/" + CLAIMS["sub"] + "/book-1/source.pdf",
    )
    book.status = BookStatus.EXTRACTING
    repo.save(book)

    response = authed_app_client.post("/books/book-1/upload-url")
    assert response.status_code == 409


def test_reissue_upload_url_for_nonexistent_book_returns_404(authed_app_client, pdf_bucket) -> None:
    response = authed_app_client.post("/books/no-such-book/upload-url")
    assert response.status_code == 404


def test_reissue_upload_url_for_another_users_book_returns_404(
    authed_app_client, pdf_bucket, dynamodb_table
) -> None:
    repo = _book_repo(dynamodb_table)
    seed_book(repo, id="book-1", user_id="someone-else", title="Not Mine")

    response = authed_app_client.post("/books/book-1/upload-url")
    assert response.status_code == 404


# --- GET /books ------------------------------------------------------------------


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
        "failureReason": None,
        "createdAt": newer.isoformat(),
        "updatedAt": newer.isoformat(),
    }


def test_list_books_anonymous_returns_401(app_client) -> None:
    response = app_client.get("/books")
    assert response.status_code == 401


# --- GET /books/{id} --------------------------------------------------------------


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


# --- GET /books/{id}/chunks --------------------------------------------------------


def test_list_book_chunks_returns_200_with_chunk_shape(
    authed_app_client, dynamodb_table
) -> None:
    book_repo = _book_repo(dynamodb_table)
    seed_book(book_repo, id="book-1", user_id=CLAIMS["sub"], title="Mine")
    from src.contexts.library.infrastructure.dynamodb_chunk_repository import (
        DynamoDbChunkRepository,
    )

    chunk_repo = DynamoDbChunkRepository(table=dynamodb_table)
    seed_chunks(chunk_repo, book_id="book-1", user_id=CLAIMS["sub"], count=2)

    response = authed_app_client.get("/books/book-1/chunks")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert body[0]["index"] == 0
    assert body[0]["status"] == "PENDING"
    assert "pageStart" in body[0]
    assert "pageEnd" in body[0]


def test_list_book_chunks_for_book_with_no_chunks_returns_empty_array(
    authed_app_client, dynamodb_table
) -> None:
    book_repo = _book_repo(dynamodb_table)
    seed_book(book_repo, id="book-1", user_id=CLAIMS["sub"], title="Mine")

    response = authed_app_client.get("/books/book-1/chunks")
    assert response.status_code == 200
    assert response.json() == []


def test_list_book_chunks_for_another_users_book_returns_404(
    authed_app_client, dynamodb_table
) -> None:
    book_repo = _book_repo(dynamodb_table)
    seed_book(book_repo, id="book-1", user_id="someone-else", title="Not Mine")

    response = authed_app_client.get("/books/book-1/chunks")
    assert response.status_code == 404


def test_list_book_chunks_for_nonexistent_book_returns_404(authed_app_client) -> None:
    response = authed_app_client.get("/books/no-such-book/chunks")
    assert response.status_code == 404
