from __future__ import annotations

from datetime import UTC, datetime
from typing import Iterator

import boto3
import pytest
from fastapi.testclient import TestClient

from src.contexts.library.domain.value_objects import BookStatus
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
        "chunksFailed": 0,
        "pageCount": 0,
        "failureReason": None,
        "audioKey": None,
        "manifestKey": None,
        "audioDurationMs": 0,
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


# --- GET /books/{id}/status (PLANS/phase-5.md §8) ----------------------------


def test_get_book_status_for_a_non_terminal_book(authed_app_client, dynamodb_table) -> None:
    repo = _book_repo(dynamodb_table)
    seed_book(repo, id="book-1", user_id=CLAIMS["sub"], title="A Book")
    repo.update_status(
        CLAIMS["sub"], "book-1", BookStatus.EXTRACTED, chunks_total=4, chunks_done=1, chunks_failed=0
    )

    response = authed_app_client.get("/books/book-1/status")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "book-1"
    assert body["status"] == "EXTRACTED"
    assert body["terminal"] is False
    assert body["progress"] == {
        "chunksTotal": 4,
        "chunksDone": 1,
        "chunksFailed": 0,
        "percent": 25,
    }
    assert body["failureReason"] is None
    assert body["audio"] == {"audioKey": None, "manifestKey": None, "durationMs": 0}
    assert "updatedAt" in body


def test_get_book_status_for_a_ready_book(authed_app_client, dynamodb_table) -> None:
    repo = _book_repo(dynamodb_table)
    seed_book(repo, id="book-1", user_id=CLAIMS["sub"])
    repo.update_status(
        CLAIMS["sub"], "book-1", BookStatus.READY,
        chunks_total=2, chunks_done=2, chunks_failed=0,
        audio_key="audio/u/book-1/book.mp3",
        manifest_key="marks/u/book-1/book.json",
        audio_duration_ms=192,
    )

    body = authed_app_client.get("/books/book-1/status").json()

    assert body["status"] == "READY"
    assert body["terminal"] is True
    assert body["progress"]["percent"] == 100
    assert body["audio"] == {
        "audioKey": "audio/u/book-1/book.mp3",
        "manifestKey": "marks/u/book-1/book.json",
        "durationMs": 192,
    }


def test_get_book_status_for_a_partial_book_is_terminal(authed_app_client, dynamodb_table) -> None:
    """Without the server-computed `terminal` flag a poller's stop condition
    is `status == "READY"`, which hangs forever on a PARTIAL book -- i.e. on
    every book in local dev and every PR environment."""
    repo = _book_repo(dynamodb_table)
    seed_book(repo, id="book-1", user_id=CLAIMS["sub"])
    repo.update_status(
        CLAIMS["sub"], "book-1", BookStatus.PARTIAL,
        chunks_total=3, chunks_done=3, chunks_failed=3,
        manifest_key="marks/u/book-1/book.json",
        failure_reason="NO_AUDIO",
    )

    body = authed_app_client.get("/books/book-1/status").json()

    assert body["status"] == "PARTIAL"
    assert body["terminal"] is True
    assert body["failureReason"] == "NO_AUDIO"
    assert body["progress"]["percent"] == 100
    assert body["audio"]["audioKey"] is None
    assert body["audio"]["manifestKey"].endswith("/book.json")


def test_get_book_status_zero_chunks_total_non_terminal_is_zero_percent(
    authed_app_client, dynamodb_table
) -> None:
    repo = _book_repo(dynamodb_table)
    seed_book(repo, id="book-1", user_id=CLAIMS["sub"])

    body = authed_app_client.get("/books/book-1/status").json()

    assert body["status"] == "UPLOADED"
    assert body["progress"]["percent"] == 0


def test_get_book_status_zero_chunks_total_terminal_is_one_hundred_percent(
    authed_app_client, dynamodb_table
) -> None:
    """A book that failed extraction has chunksTotal == 0 and must not render
    as a 0%-forever progress bar."""
    repo = _book_repo(dynamodb_table)
    seed_book(repo, id="book-1", user_id=CLAIMS["sub"])
    repo.update_status(CLAIMS["sub"], "book-1", BookStatus.FAILED, failure_reason="NO_TEXT_LAYER")

    body = authed_app_client.get("/books/book-1/status").json()

    assert body["terminal"] is True
    assert body["progress"]["percent"] == 100
    assert body["failureReason"] == "NO_TEXT_LAYER"


def test_get_book_status_agrees_with_get_book(authed_app_client, dynamodb_table) -> None:
    repo = _book_repo(dynamodb_table)
    seed_book(repo, id="book-1", user_id=CLAIMS["sub"])
    repo.update_status(
        CLAIMS["sub"], "book-1", BookStatus.READY,
        chunks_total=1, chunks_done=1,
        audio_key="audio/u/book-1/book.mp3",
        manifest_key="marks/u/book-1/book.json",
        audio_duration_ms=500,
    )

    status_body = authed_app_client.get("/books/book-1/status").json()
    book_body = authed_app_client.get("/books/book-1").json()

    assert status_body["status"] == book_body["status"]
    assert status_body["audio"]["audioKey"] == book_body["audioKey"]
    assert status_body["audio"]["manifestKey"] == book_body["manifestKey"]
    assert status_body["audio"]["durationMs"] == book_body["audioDurationMs"]


def test_get_book_status_for_another_users_book_returns_404(
    authed_app_client, dynamodb_table
) -> None:
    """404, never 403 -- the repo's standing rule."""
    seed_book(_book_repo(dynamodb_table), id="book-1", user_id="someone-else")
    assert authed_app_client.get("/books/book-1/status").status_code == 404


def test_get_book_status_for_a_nonexistent_book_returns_404(authed_app_client) -> None:
    assert authed_app_client.get("/books/ghost/status").status_code == 404


def test_get_book_status_anonymous_returns_401(app_client) -> None:
    assert app_client.get("/books/book-1/status").status_code == 401
