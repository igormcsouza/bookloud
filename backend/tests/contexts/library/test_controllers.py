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


# --- DELETE /books/{id} -----------------------------------------------------------


def test_delete_book_returns_204_and_removes_the_book(
    authed_app_client, pdf_bucket, audio_buckets, dynamodb_table
) -> None:
    repo = _book_repo(dynamodb_table)
    seed_book(repo, id="book-1", user_id=CLAIMS["sub"], title="Mine")

    response = authed_app_client.delete("/books/book-1")
    assert response.status_code == 204

    assert repo.get(CLAIMS["sub"], "book-1") is None
    assert authed_app_client.get("/books/book-1").status_code == 404


def test_delete_book_removes_source_pdf_and_stitched_outputs(
    authed_app_client, pdf_bucket, audio_buckets, dynamodb_table
) -> None:
    from src.contexts.library.domain.book import Book

    source_key = f"books/{CLAIMS['sub']}/book-1/source.pdf"
    pdf_bucket.put_object(Bucket=PDF_BUCKET, Key=source_key, Body=b"%PDF-1.4")
    audio_key = f"audio/{CLAIMS['sub']}/book-1/book.mp3"
    manifest_key = f"marks/{CLAIMS['sub']}/book-1/book.json"
    audio_buckets.put_object(Bucket=AUDIO_BUCKET, Key=audio_key, Body=b"\xff\xf3\x00\x00")
    audio_buckets.put_object(Bucket=MARKS_BUCKET, Key=manifest_key, Body=b"{}")

    repo = _book_repo(dynamodb_table)
    book = Book.create(
        id="book-1", user_id=CLAIMS["sub"], title_raw="Mine", now=datetime.now(UTC),
        source_key=source_key,
    )
    repo.save(book)

    response = authed_app_client.delete("/books/book-1")
    assert response.status_code == 204

    with pytest.raises(Exception):
        pdf_bucket.get_object(Bucket=PDF_BUCKET, Key=source_key)
    with pytest.raises(Exception):
        audio_buckets.get_object(Bucket=AUDIO_BUCKET, Key=audio_key)
    with pytest.raises(Exception):
        audio_buckets.get_object(Bucket=MARKS_BUCKET, Key=manifest_key)


def test_delete_book_for_another_users_book_returns_404(
    authed_app_client, pdf_bucket, audio_buckets, dynamodb_table
) -> None:
    repo = _book_repo(dynamodb_table)
    seed_book(repo, id="book-1", user_id="someone-else", title="Not Mine")

    response = authed_app_client.delete("/books/book-1")
    assert response.status_code == 404
    assert repo.get("someone-else", "book-1") is not None


def test_delete_book_for_nonexistent_id_returns_404(
    authed_app_client, pdf_bucket, audio_buckets
) -> None:
    response = authed_app_client.delete("/books/no-such-book")
    assert response.status_code == 404


def test_delete_book_anonymous_returns_401(app_client, pdf_bucket, audio_buckets) -> None:
    response = app_client.delete("/books/book-1")
    assert response.status_code == 401


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


# --- phase 6: audio delivery (PLANS/phase-6.md §4.3, §13.2) --------------------

AUDIO_BUCKET = "bookloud-test-audio"
MARKS_BUCKET = "bookloud-test-marks"
MANIFEST_DOC = b'{"version":1,"bookId":"book-1","durationMs":4200,"segments":[],"missing":[0]}'
MARKS_DOC = b'{"version":1,"chunkIndex":1,"words":[{"t":0,"d":10,"s":0,"e":3,"w":"The"}]}'


@pytest.fixture
def audio_buckets(dynamodb_table, monkeypatch: pytest.MonkeyPatch):
    """Real (moto) S3 buckets behind ``settings.audio_bucket``/
    ``settings.marks_bucket``, mirroring the ``pdf_bucket`` fixture. The
    delivery routes build ``S3AudioDelivery``/``S3ObjectStorage`` per request,
    so no ``dependency_overrides`` are needed."""
    import src.config as config

    monkeypatch.setattr(config.settings, "audio_bucket", AUDIO_BUCKET)
    monkeypatch.setattr(config.settings, "marks_bucket", MARKS_BUCKET)
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=AUDIO_BUCKET)
    client.create_bucket(Bucket=MARKS_BUCKET)
    return client


def _seed_stitched_book(dynamodb_table, s3, *, with_marks: bool = True):
    """A READY book with a stitched book.mp3, a manifest and one DONE chunk
    carrying its own audio/marks."""
    from src.contexts.library.domain.value_objects import ChunkStatus

    repo = _book_repo(dynamodb_table)
    seed_book(repo, id="book-1", user_id=CLAIMS["sub"])
    chunk_repo = _chunk_repo(dynamodb_table)
    seed_chunks(chunk_repo, book_id="book-1", user_id=CLAIMS["sub"], count=3)

    audio_key = f"audio/{CLAIMS['sub']}/book-1/book.mp3"
    manifest_key = f"marks/{CLAIMS['sub']}/book-1/book.json"
    chunk_audio_key = f"audio/{CLAIMS['sub']}/book-1/000001.mp3"
    chunk_marks_key = f"marks/{CLAIMS['sub']}/book-1/000001.json"

    s3.put_object(Bucket=AUDIO_BUCKET, Key=audio_key, Body=b"\xff\xf3\x00\x00")
    s3.put_object(Bucket=MARKS_BUCKET, Key=manifest_key, Body=MANIFEST_DOC)
    if with_marks:
        s3.put_object(Bucket=MARKS_BUCKET, Key=chunk_marks_key, Body=MARKS_DOC)

    chunk_repo.update_status(
        "book-1",
        1,
        ChunkStatus.DONE,
        audio_key=chunk_audio_key,
        marks_key=chunk_marks_key if with_marks else None,
        duration_ms=1500,
    )
    repo.update_status(
        CLAIMS["sub"],
        "book-1",
        BookStatus.READY,
        chunks_total=3,
        chunks_done=3,
        audio_key=audio_key,
        manifest_key=manifest_key,
        audio_duration_ms=4200,
    )


def _chunk_repo(dynamodb_table):
    from src.contexts.library.infrastructure.dynamodb_chunk_repository import (
        DynamoDbChunkRepository,
    )

    return DynamoDbChunkRepository(table=dynamodb_table)


def test_get_book_audio_returns_a_presigned_url(
    authed_app_client, audio_buckets, dynamodb_table
) -> None:
    _seed_stitched_book(dynamodb_table, audio_buckets)

    response = authed_app_client.get("/books/book-1/audio")

    assert response.status_code == 200
    body = response.json()
    assert sorted(body) == ["contentType", "durationMs", "expiresIn", "url"]
    assert "X-Amz-Signature=" in body["url"]
    assert body["expiresIn"] == 3600
    # From the DynamoDB row, never from the file (PLANS/phase-5.md §7.1).
    assert body["durationMs"] == 4200
    assert body["contentType"] == "audio/mpeg"


def test_get_book_audio_without_audio_returns_409(
    authed_app_client, audio_buckets, dynamodb_table
) -> None:
    """The everyday state of every non-prod environment (phase-4 §0)."""
    seed_book(_book_repo(dynamodb_table), id="book-1", user_id=CLAIMS["sub"])
    assert authed_app_client.get("/books/book-1/audio").status_code == 409


def test_get_book_audio_for_another_users_book_returns_404(
    authed_app_client, audio_buckets, dynamodb_table
) -> None:
    seed_book(_book_repo(dynamodb_table), id="book-1", user_id="someone-else")
    assert authed_app_client.get("/books/book-1/audio").status_code == 404


def test_get_book_audio_anonymous_returns_401(app_client) -> None:
    assert app_client.get("/books/book-1/audio").status_code == 401


def test_get_chunk_audio_returns_a_presigned_url(
    authed_app_client, audio_buckets, dynamodb_table
) -> None:
    _seed_stitched_book(dynamodb_table, audio_buckets)

    response = authed_app_client.get("/books/book-1/chunks/1/audio")

    assert response.status_code == 200
    body = response.json()
    assert "X-Amz-Signature=" in body["url"]
    assert "000001.mp3" in body["url"]
    assert body["durationMs"] == 1500


def test_get_chunk_audio_without_audio_returns_409(
    authed_app_client, audio_buckets, dynamodb_table
) -> None:
    _seed_stitched_book(dynamodb_table, audio_buckets)
    # Chunk 0 never synthesized.
    assert authed_app_client.get("/books/book-1/chunks/0/audio").status_code == 409


def test_get_chunk_audio_for_a_missing_chunk_returns_404(
    authed_app_client, audio_buckets, dynamodb_table
) -> None:
    _seed_stitched_book(dynamodb_table, audio_buckets)
    assert authed_app_client.get("/books/book-1/chunks/99/audio").status_code == 404


def test_get_chunk_audio_for_another_users_book_returns_404(
    authed_app_client, audio_buckets, dynamodb_table
) -> None:
    seed_book(_book_repo(dynamodb_table), id="book-1", user_id="someone-else")
    assert authed_app_client.get("/books/book-1/chunks/1/audio").status_code == 404


def test_get_chunk_audio_anonymous_returns_401(app_client) -> None:
    assert app_client.get("/books/book-1/chunks/1/audio").status_code == 401


def test_get_book_manifest_returns_the_object_verbatim(
    authed_app_client, audio_buckets, dynamodb_table
) -> None:
    _seed_stitched_book(dynamodb_table, audio_buckets)

    response = authed_app_client.get("/books/book-1/manifest")

    assert response.status_code == 200
    # Byte-for-byte: the document is the contract (PLANS/phase-5.md §7.2).
    assert response.content == MANIFEST_DOC
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "private, max-age=3600"


def test_get_book_manifest_without_a_manifest_key_returns_404(
    authed_app_client, audio_buckets, dynamodb_table
) -> None:
    seed_book(_book_repo(dynamodb_table), id="book-1", user_id=CLAIMS["sub"])
    assert authed_app_client.get("/books/book-1/manifest").status_code == 404


def test_get_book_manifest_with_the_object_deleted_returns_404(
    authed_app_client, audio_buckets, dynamodb_table
) -> None:
    """A PR bucket torn down under a still-open tab -- "text only, no audio",
    not a crash (§9's degradation table)."""
    _seed_stitched_book(dynamodb_table, audio_buckets)
    audio_buckets.delete_object(
        Bucket=MARKS_BUCKET, Key=f"marks/{CLAIMS['sub']}/book-1/book.json"
    )
    assert authed_app_client.get("/books/book-1/manifest").status_code == 404


def test_get_book_manifest_for_another_users_book_returns_404(
    authed_app_client, audio_buckets, dynamodb_table
) -> None:
    seed_book(_book_repo(dynamodb_table), id="book-1", user_id="someone-else")
    assert authed_app_client.get("/books/book-1/manifest").status_code == 404


def test_get_book_manifest_anonymous_returns_401(app_client) -> None:
    assert app_client.get("/books/book-1/manifest").status_code == 401


def test_get_chunk_marks_returns_the_object_verbatim(
    authed_app_client, audio_buckets, dynamodb_table
) -> None:
    _seed_stitched_book(dynamodb_table, audio_buckets)

    response = authed_app_client.get("/books/book-1/chunks/1/marks")

    assert response.status_code == 200
    assert response.content == MARKS_DOC
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "private, max-age=86400"


def test_get_chunk_marks_without_a_marks_key_returns_404(
    authed_app_client, audio_buckets, dynamodb_table
) -> None:
    """The normal case for every chunk in a PR environment -- which is why
    the client negatively caches it instead of retrying."""
    _seed_stitched_book(dynamodb_table, audio_buckets)
    assert authed_app_client.get("/books/book-1/chunks/0/marks").status_code == 404


def test_get_chunk_marks_for_another_users_book_returns_404(
    authed_app_client, audio_buckets, dynamodb_table
) -> None:
    seed_book(_book_repo(dynamodb_table), id="book-1", user_id="someone-else")
    assert authed_app_client.get("/books/book-1/chunks/1/marks").status_code == 404


def test_get_chunk_marks_anonymous_returns_401(app_client) -> None:
    assert app_client.get("/books/book-1/chunks/1/marks").status_code == 401


# --- phase 6: POST /books/{id}/resynthesize (PLANS/phase-6.md §5) --------------


@pytest.fixture
def fake_queues(dynamodb_table):
    """Recording fakes behind the two SQS providers.

    The one place in this file that needs ``dependency_overrides``: every
    other adapter is pointed at moto by monkeypatching a *setting*, but SQS
    has no URL to point anywhere -- ``api_stack.py`` grants the API Lambda
    ``sqs:SendMessage`` on both real queues this phase, and there is nothing
    for moto to intercept without one. Overriding the provider also lets the
    test assert the exact published payload, which is the part that matters."""
    from src.contexts.library.interface.dependencies import (
        get_stitch_queue,
        get_synthesis_queue,
    )
    from src.main import app
    from tests.contexts.library.fakes import FakeStitchQueue, FakeSynthesisQueue

    synthesis, stitch = FakeSynthesisQueue(), FakeStitchQueue()
    app.dependency_overrides[get_synthesis_queue] = lambda: synthesis
    app.dependency_overrides[get_stitch_queue] = lambda: stitch
    try:
        yield synthesis, stitch
    finally:
        app.dependency_overrides.pop(get_synthesis_queue, None)
        app.dependency_overrides.pop(get_stitch_queue, None)


def _seed_partial_book(dynamodb_table, *, failed: tuple[int, ...] = (1, 2), reason="NO_AUDIO"):
    from src.contexts.library.domain.value_objects import ChunkStatus

    repo = _book_repo(dynamodb_table)
    chunk_repo = _chunk_repo(dynamodb_table)
    seed_book(repo, id="book-1", user_id=CLAIMS["sub"])
    seed_chunks(chunk_repo, book_id="book-1", user_id=CLAIMS["sub"], count=3)
    for index in range(3):
        if index in failed:
            chunk_repo.update_status(
                "book-1", index, ChunkStatus.FAILED, failure_reason="EXTERNAL_TTS_DISABLED"
            )
        else:
            chunk_repo.update_status(
                "book-1",
                index,
                ChunkStatus.DONE,
                audio_key=f"audio/{CLAIMS['sub']}/book-1/{index:06d}.mp3",
                duration_ms=1000,
            )
    repo.update_status(
        CLAIMS["sub"],
        "book-1",
        BookStatus.PARTIAL,
        chunks_total=3,
        chunks_done=3,
        chunks_failed=len(failed),
        manifest_key=f"marks/{CLAIMS['sub']}/book-1/book.json",
        failure_reason=reason,
    )


def test_resynthesize_returns_202_with_the_rewound_book(
    authed_app_client, dynamodb_table, fake_queues
) -> None:
    _seed_partial_book(dynamodb_table)
    synthesis, _stitch = fake_queues

    response = authed_app_client.post("/books/book-1/resynthesize")

    assert response.status_code == 202
    body = response.json()
    assert body["retriedChunks"] == 2
    assert body["republishedStitch"] is False
    # The client drops `book` straight into its poll state, so it must be
    # book_status_to_dict's exact shape.
    assert body["book"]["status"] == "EXTRACTED"
    assert body["book"]["terminal"] is False
    assert body["book"]["progress"]["chunksDone"] == 1
    assert body["book"]["progress"]["chunksFailed"] == 0
    assert body["book"]["audio"]["audioKey"] is None
    assert synthesis.calls == [
        {"user_id": CLAIMS["sub"], "book_id": "book-1", "chunk_indexes": [1, 2]}
    ]


def test_resynthesize_a_stitch_failed_book_publishes_a_stitch(
    authed_app_client, dynamodb_table, fake_queues
) -> None:
    _seed_partial_book(dynamodb_table, failed=(), reason="STITCH_FAILED")
    synthesis, stitch = fake_queues

    response = authed_app_client.post("/books/book-1/resynthesize")

    assert response.status_code == 202
    assert response.json()["retriedChunks"] == 0
    assert response.json()["republishedStitch"] is True
    assert synthesis.calls == []
    assert stitch.calls == [{"user_id": CLAIMS["sub"], "book_id": "book-1"}]


@pytest.mark.parametrize(
    "status", ["UPLOADED", "EXTRACTING", "EXTRACTED", "STITCHING", "READY", "FAILED"]
)
def test_resynthesize_a_non_partial_book_returns_409(
    authed_app_client, dynamodb_table, fake_queues, status: str
) -> None:
    repo = _book_repo(dynamodb_table)
    seed_book(repo, id="book-1", user_id=CLAIMS["sub"])
    repo.update_status(CLAIMS["sub"], "book-1", BookStatus(status), chunks_total=3)

    assert authed_app_client.post("/books/book-1/resynthesize").status_code == 409


def test_resynthesize_twice_returns_202_then_409(
    authed_app_client, dynamodb_table, fake_queues
) -> None:
    """The conditional book update is the exactly-once gate -- a
    double-clicked button produces one 202 and one 409."""
    _seed_partial_book(dynamodb_table)

    assert authed_app_client.post("/books/book-1/resynthesize").status_code == 202
    assert authed_app_client.post("/books/book-1/resynthesize").status_code == 409


def test_resynthesize_another_users_book_returns_404(
    authed_app_client, dynamodb_table, fake_queues
) -> None:
    seed_book(_book_repo(dynamodb_table), id="book-1", user_id="someone-else")
    assert authed_app_client.post("/books/book-1/resynthesize").status_code == 404


def test_resynthesize_a_nonexistent_book_returns_404(
    authed_app_client, dynamodb_table, fake_queues
) -> None:
    assert authed_app_client.post("/books/ghost/resynthesize").status_code == 404


def test_resynthesize_anonymous_returns_401(app_client) -> None:
    assert app_client.post("/books/book-1/resynthesize").status_code == 401
