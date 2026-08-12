"""``S3AudioDelivery`` + ``public_s3_client`` (PLANS/phase-6.md §4.2/§13.2).

The endpoint-selection tests moved here from ``test_s3_pdf_storage.py`` when
``_build_client`` was promoted to ``infrastructure/s3_client.py``: the bug
they guard against (a well-formed, correctly-signed URL pointing at a
hostname that only resolves inside the compose network) is now shared by the
presigned upload *and* the presigned download, so it gets one home.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.config import Config
from moto import mock_aws

from src.contexts.library.domain.storage import AUDIO_URL_EXPIRES_IN, PresignedDownload
from src.contexts.library.infrastructure.s3_audio_delivery import S3AudioDelivery
from src.contexts.library.infrastructure.s3_client import SIGNATURE_VERSION_V4

BUCKET = "bookloud-test-audio"
KEY = "audio/user-1/book-1/book.mp3"


@pytest.fixture
def s3_bucket(monkeypatch: pytest.MonkeyPatch):
    import src.config as config

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.setattr(config.settings, "aws_endpoint_url", "")
    monkeypatch.setattr(config.settings, "s3_public_endpoint_url", "")

    with mock_aws():
        # SigV4 explicitly, matching what public_s3_client(signature_version=
        # SIGNATURE_VERSION_V4) builds in production. A default-config client
        # here would presign with the legacy SigV2 query string and every
        # X-Amz-* assertion below would be testing moto's default rather than
        # the adapter's contract.
        client = boto3.client(
            "s3", region_name="us-east-1", config=Config(signature_version=SIGNATURE_VERSION_V4)
        )
        client.create_bucket(Bucket=BUCKET)
        client.put_object(Bucket=BUCKET, Key=KEY, Body=b"\xff\xf3\x00\x00")
        yield client


# --- the presigned URL itself ----------------------------------------------


def test_presigned_download_url_carries_bucket_key_and_signature(s3_bucket) -> None:
    delivery = S3AudioDelivery(bucket=BUCKET, client=s3_bucket)

    result = delivery.presigned_download(key=KEY)

    assert isinstance(result, PresignedDownload)
    assert BUCKET in result.url
    assert KEY in result.url
    assert "X-Amz-Signature=" in result.url


def test_presigned_download_defaults_to_one_hour(s3_bucket) -> None:
    delivery = S3AudioDelivery(bucket=BUCKET, client=s3_bucket)

    result = delivery.presigned_download(key=KEY)

    assert result.expires_in == AUDIO_URL_EXPIRES_IN == 3600
    query = parse_qs(urlparse(result.url).query)
    assert query["X-Amz-Expires"] == ["3600"]


def test_expires_in_reaches_the_query_string(s3_bucket) -> None:
    delivery = S3AudioDelivery(bucket=BUCKET, client=s3_bucket)

    result = delivery.presigned_download(key=KEY, expires_in=120)

    assert result.expires_in == 120
    assert parse_qs(urlparse(result.url).query)["X-Amz-Expires"] == ["120"]


def test_presigning_does_not_require_the_object_to_exist(s3_bucket) -> None:
    """Signing is purely local -- no HeadObject, no network call. Callers
    guard on the DynamoDB row's audioKey instead (application/delivery.py),
    which is what keeps the 409 authoritative."""
    delivery = S3AudioDelivery(bucket=BUCKET, client=s3_bucket)

    result = delivery.presigned_download(key="audio/user-1/book-1/does-not-exist.mp3")

    assert "X-Amz-Signature=" in result.url


def test_range_is_not_in_signed_headers(s3_bucket) -> None:
    """The load-bearing property behind ``<audio>`` seeking: SigV4
    query-string presigning signs only ``host``, so S3 honours whatever
    ``Range`` the browser sends. ``local/smoke_test.py`` proves the other
    half (a real 206 from a real S3 API) -- no mock can."""
    delivery = S3AudioDelivery(bucket=BUCKET, client=s3_bucket)

    query = parse_qs(urlparse(delivery.presigned_download(key=KEY).url).query)

    assert query["X-Amz-SignedHeaders"] == ["host"]


def test_default_constructor_builds_a_real_client(s3_bucket) -> None:
    # No `client=` kwarg -- exercises the public_s3_client() default path
    # (moto intercepts the resulting boto3 client transparently).
    delivery = S3AudioDelivery(bucket=BUCKET)

    assert "X-Amz-Signature=" in delivery.presigned_download(key=KEY).url


# --- public_s3_client(): the compose landmine (§4.2) ------------------------


def _fake_boto3(captured: dict):
    class FakeBoto3:
        @staticmethod
        def client(service, **kwargs):
            captured["service"] = service
            captured["kwargs"] = kwargs
            return "fake-client"

    return FakeBoto3


def test_public_client_uses_s3_public_endpoint_url_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.config as config
    from src.contexts.library.infrastructure.s3_client import public_s3_client

    captured: dict = {}
    monkeypatch.setattr(config.settings, "s3_public_endpoint_url", "http://localhost:4566")
    monkeypatch.setattr(config.settings, "aws_endpoint_url", "http://localstack:4566")
    monkeypatch.setattr(
        "src.contexts.library.infrastructure.s3_client.boto3", _fake_boto3(captured)
    )

    assert public_s3_client() == "fake-client"
    assert captured["service"] == "s3"
    # The whole point: localstack:4566 resolves only on the compose network.
    assert captured["kwargs"]["endpoint_url"] == "http://localhost:4566"


def test_public_client_falls_back_to_aws_endpoint_url(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.config as config
    from src.contexts.library.infrastructure.s3_client import public_s3_client

    captured: dict = {}
    monkeypatch.setattr(config.settings, "s3_public_endpoint_url", "")
    monkeypatch.setattr(config.settings, "aws_endpoint_url", "http://localstack:4566")
    monkeypatch.setattr(
        "src.contexts.library.infrastructure.s3_client.boto3", _fake_boto3(captured)
    )

    public_s3_client()

    assert captured["kwargs"]["endpoint_url"] == "http://localstack:4566"


def test_public_client_passes_no_endpoint_in_real_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.config as config
    from src.contexts.library.infrastructure.s3_client import public_s3_client

    captured: dict = {}
    monkeypatch.setattr(config.settings, "s3_public_endpoint_url", "")
    monkeypatch.setattr(config.settings, "aws_endpoint_url", "")
    monkeypatch.setattr(
        "src.contexts.library.infrastructure.s3_client.boto3", _fake_boto3(captured)
    )

    public_s3_client()

    assert "endpoint_url" not in captured["kwargs"]


def test_construction_with_no_region_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """The phase-4/5 ``NoRegionError`` regression shape, applied to the one
    adapter this phase adds. S3 has a global endpoint, so this one may be
    eager where the SQS adapters had to be lazy -- but "may" is a claim, and
    the backend test job runs with no credentials and no region at all, so it
    gets asserted rather than believed."""
    import src.config as config

    for var in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config.settings, "s3_public_endpoint_url", "")
    monkeypatch.setattr(config.settings, "aws_endpoint_url", "")

    delivery = S3AudioDelivery(bucket=BUCKET)

    assert delivery is not None
