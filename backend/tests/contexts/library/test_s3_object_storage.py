from __future__ import annotations

from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from src.contexts.library.infrastructure.s3_object_storage import S3ObjectStorage
from src.shared_kernel.domain.errors import NotFoundError

BUCKET = "bookloud-test-audio"


@pytest.fixture
def s3_bucket(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def test_put_bytes_then_get_bytes_round_trips(s3_bucket) -> None:
    storage = S3ObjectStorage(bucket=BUCKET, s3_client=s3_bucket)
    storage.put_bytes(key="audio/u/b/000000.mp3", data=b"fake-mp3-bytes", content_type="audio/mpeg")

    fetched = storage.get_bytes(key="audio/u/b/000000.mp3")

    assert fetched == b"fake-mp3-bytes"


def test_put_bytes_sets_content_type(s3_bucket) -> None:
    storage = S3ObjectStorage(bucket=BUCKET, s3_client=s3_bucket)
    storage.put_bytes(key="marks/u/b/000000.json", data=b'{"a":1}', content_type="application/json")

    head = s3_bucket.head_object(Bucket=BUCKET, Key="marks/u/b/000000.json")
    assert head["ContentType"] == "application/json"


def test_get_bytes_missing_key_raises_not_found(s3_bucket) -> None:
    storage = S3ObjectStorage(bucket=BUCKET, s3_client=s3_bucket)
    with pytest.raises(NotFoundError):
        storage.get_bytes(key="audio/u/b/999999.mp3")


def test_put_bytes_twice_overwrites(s3_bucket) -> None:
    storage = S3ObjectStorage(bucket=BUCKET, s3_client=s3_bucket)
    storage.put_bytes(key="audio/u/b/000000.mp3", data=b"first", content_type="audio/mpeg")
    storage.put_bytes(key="audio/u/b/000000.mp3", data=b"second", content_type="audio/mpeg")
    assert storage.get_bytes(key="audio/u/b/000000.mp3") == b"second"


def test_get_bytes_reraises_non_404_client_errors() -> None:
    stub_client = MagicMock()
    stub_client.get_object.side_effect = ClientError(
        error_response={"Error": {"Code": "AccessDenied", "Message": "nope"}},
        operation_name="GetObject",
    )
    storage = S3ObjectStorage(bucket=BUCKET, s3_client=stub_client)
    with pytest.raises(ClientError):
        storage.get_bytes(key="audio/u/b/000000.mp3")
