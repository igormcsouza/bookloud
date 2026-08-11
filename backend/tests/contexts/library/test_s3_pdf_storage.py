from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from src.contexts.library.infrastructure.s3_pdf_storage import S3PdfStorage
from src.shared_kernel.domain.errors import NotFoundError

BUCKET = "bookloud-test-pdfs"


@pytest.fixture
def s3_bucket(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "test")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def storage(s3_bucket) -> S3PdfStorage:
    return S3PdfStorage(bucket=BUCKET, client=s3_bucket)


# --- presigned_upload --------------------------------------------------------


def test_presigned_upload_returns_url_and_fields(storage: S3PdfStorage) -> None:
    upload = storage.presigned_upload(key="books/user-1/book-1/source.pdf")

    assert upload.key == "books/user-1/book-1/source.pdf"
    assert upload.url
    assert upload.fields["key"] == "books/user-1/book-1/source.pdf"
    assert upload.fields["Content-Type"] == "application/pdf"
    assert "policy" in upload.fields
    assert "signature" in upload.fields


def test_presigned_upload_uses_configured_expiry_and_max_bytes(s3_bucket) -> None:
    storage = S3PdfStorage(bucket=BUCKET, client=s3_bucket, expires_in=123, max_bytes=999)
    upload = storage.presigned_upload(key="books/user-1/book-1/source.pdf")
    assert upload.expires_in == 123
    assert upload.max_bytes == 999


def test_presigned_upload_pins_exact_key_no_wildcards(storage: S3PdfStorage) -> None:
    # No `${filename}`/`starts-with` -- the policy document embeds the exact
    # key, verifiable by decoding the base64 policy field.
    import base64
    import json

    upload = storage.presigned_upload(key="books/user-1/book-1/source.pdf")
    policy = json.loads(base64.b64decode(upload.fields["policy"]))
    key_conditions = [c for c in policy["conditions"] if isinstance(c, dict) and "key" in c]
    assert key_conditions == [{"key": "books/user-1/book-1/source.pdf"}]


def test_presigned_upload_includes_content_length_range_condition(storage: S3PdfStorage) -> None:
    import base64
    import json

    upload = storage.presigned_upload(key="books/user-1/book-1/source.pdf")
    policy = json.loads(base64.b64decode(upload.fields["policy"]))
    range_conditions = [
        c for c in policy["conditions"] if isinstance(c, list) and c[0] == "content-length-range"
    ]
    assert len(range_conditions) == 1
    assert range_conditions[0][1:] == [0, upload.max_bytes]


# --- get_bytes ----------------------------------------------------------------


def test_get_bytes_round_trips(storage: S3PdfStorage, s3_bucket) -> None:
    s3_bucket.put_object(Bucket=BUCKET, Key="books/user-1/book-1/source.pdf", Body=b"%PDF-1.4 fake bytes")
    data = storage.get_bytes(key="books/user-1/book-1/source.pdf")
    assert data == b"%PDF-1.4 fake bytes"


def test_get_bytes_missing_key_raises_not_found(storage: S3PdfStorage) -> None:
    with pytest.raises(NotFoundError):
        storage.get_bytes(key="books/user-1/book-1/source.pdf")


def test_get_bytes_reraises_other_client_errors(monkeypatch: pytest.MonkeyPatch, s3_bucket) -> None:
    from unittest.mock import MagicMock

    from botocore.exceptions import ClientError

    stub_client = MagicMock()
    stub_client.get_object.side_effect = ClientError(
        error_response={"Error": {"Code": "AccessDenied", "Message": "nope"}},
        operation_name="GetObject",
    )
    storage = S3PdfStorage(bucket=BUCKET, client=stub_client)
    with pytest.raises(ClientError):
        storage.get_bytes(key="books/user-1/book-1/source.pdf")


# --- client construction / endpoint override -----------------------------------
# The three endpoint-selection tests that lived here moved to
# test_s3_audio_delivery.py when `_build_client` was promoted to
# infrastructure/s3_client.py's `public_s3_client` (PLANS/phase-6.md §4.2):
# the presigned *download* has exactly the same failure mode, so the rule
# gets one home rather than two. This file keeps the default-path test below,
# which is what proves S3PdfStorage actually uses it.


def test_default_constructor_builds_a_real_client(s3_bucket) -> None:
    # No `client=` kwarg -- exercises the `public_s3_client()` default path
    # (moto intercepts the resulting boto3 client transparently).
    storage = S3PdfStorage(bucket=BUCKET)
    upload = storage.presigned_upload(key="books/user-1/book-1/source.pdf")
    assert upload.key == "books/user-1/book-1/source.pdf"
