from __future__ import annotations

from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from src.contexts.library.infrastructure.s3_object_storage import MIN_PART_BYTES, S3ObjectStorage
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


# --- open_multipart (PLANS/phase-5.md §7.4) ---------------------------------


def test_multipart_under_the_minimum_part_size_writes_one_exact_object(s3_bucket) -> None:
    storage = S3ObjectStorage(bucket=BUCKET, s3_client=s3_bucket)
    payload = b"a small stitched book" * 100

    with storage.open_multipart(key="audio/u/b/book.mp3", content_type="audio/mpeg") as writer:
        writer.write(payload)

    assert storage.get_bytes(key="audio/u/b/book.mp3") == payload
    head = s3_bucket.head_object(Bucket=BUCKET, Key="audio/u/b/book.mp3")
    assert head["ContentType"] == "audio/mpeg"


def test_multipart_over_the_minimum_part_size_across_many_small_writes_is_byte_exact(s3_bucket) -> None:
    """The whole point of the writer: memory stays O(part), not O(book). Many
    small writes must still produce exactly the concatenation."""
    storage = S3ObjectStorage(bucket=BUCKET, s3_client=s3_bucket)
    segment = b"x" * 64 * 1024  # 64 KiB
    count = 200  # 12.5 MiB -> 3 parts at a 5 MiB floor

    with storage.open_multipart(key="audio/u/b/book.mp3", content_type="audio/mpeg") as writer:
        for _ in range(count):
            writer.write(segment)
        assert writer.bytes_written == len(segment) * count

    assert storage.get_bytes(key="audio/u/b/book.mp3") == segment * count


def test_multipart_uploads_more_than_one_part_when_it_exceeds_the_floor(s3_bucket) -> None:
    storage = S3ObjectStorage(bucket=BUCKET, s3_client=s3_bucket)
    with storage.open_multipart(key="audio/u/b/book.mp3", content_type="audio/mpeg") as writer:
        writer.write(b"y" * (MIN_PART_BYTES + 1024))
        # The floor-sized part is flushed eagerly; only the tail is resident.
        assert len(writer._parts) == 1  # noqa: SLF001 -- asserting the bounded-memory behaviour

    assert len(storage.get_bytes(key="audio/u/b/book.mp3")) == MIN_PART_BYTES + 1024


def test_multipart_exception_aborts_and_leaves_no_object(s3_bucket) -> None:
    storage = S3ObjectStorage(bucket=BUCKET, s3_client=s3_bucket)

    with pytest.raises(RuntimeError):
        with storage.open_multipart(key="audio/u/b/book.mp3", content_type="audio/mpeg") as writer:
            writer.write(b"z" * (MIN_PART_BYTES + 10))
            raise RuntimeError("a chunk GET failed mid-concatenation")

    with pytest.raises(NotFoundError):
        storage.get_bytes(key="audio/u/b/book.mp3")
    assert s3_bucket.list_multipart_uploads(Bucket=BUCKET).get("Uploads", []) == []


def test_multipart_with_nothing_written_still_completes_an_empty_object(s3_bucket) -> None:
    storage = S3ObjectStorage(bucket=BUCKET, s3_client=s3_bucket)
    with storage.open_multipart(key="audio/u/b/book.mp3", content_type="audio/mpeg") as writer:
        assert writer.bytes_written == 0
    assert storage.get_bytes(key="audio/u/b/book.mp3") == b""


def test_multipart_complete_failure_aborts_the_upload() -> None:
    stub_client = MagicMock()
    stub_client.create_multipart_upload.return_value = {"UploadId": "upload-1"}
    stub_client.upload_part.return_value = {"ETag": '"etag"'}
    stub_client.complete_multipart_upload.side_effect = RuntimeError("S3 said no")
    storage = S3ObjectStorage(bucket=BUCKET, s3_client=stub_client)

    with pytest.raises(RuntimeError):
        with storage.open_multipart(key="audio/u/b/book.mp3", content_type="audio/mpeg") as writer:
            writer.write(b"data")

    stub_client.abort_multipart_upload.assert_called_once()


def test_multipart_declares_a_checksum_algorithm_on_every_call() -> None:
    """Regression guard for the "Checksum Type mismatch" InvalidRequest:
    botocore >= 1.36 attaches a CRC32 checksum to UploadPart whether we ask
    or not, so CreateMultipartUpload must declare the same algorithm or S3
    rejects the part. Caught by `make smoke` against LocalStack -- moto is
    lenient here, so only an explicit assertion keeps it fixed."""
    stub_client = MagicMock()
    stub_client.create_multipart_upload.return_value = {"UploadId": "upload-1"}
    stub_client.upload_part.return_value = {"ETag": '"etag"', "ChecksumCRC32": "abc123=="}
    storage = S3ObjectStorage(bucket=BUCKET, s3_client=stub_client)

    with storage.open_multipart(key="audio/u/b/book.mp3", content_type="audio/mpeg") as writer:
        writer.write(b"data")

    assert stub_client.create_multipart_upload.call_args.kwargs["ChecksumAlgorithm"] == "CRC32"
    assert stub_client.upload_part.call_args.kwargs["ChecksumAlgorithm"] == "CRC32"
    parts = stub_client.complete_multipart_upload.call_args.kwargs["MultipartUpload"]["Parts"]
    assert parts == [{"ETag": '"etag"', "PartNumber": 1, "ChecksumCRC32": "abc123=="}]


def test_multipart_tolerates_a_backend_that_returns_no_checksum() -> None:
    stub_client = MagicMock()
    stub_client.create_multipart_upload.return_value = {"UploadId": "upload-1"}
    stub_client.upload_part.return_value = {"ETag": '"etag"'}
    storage = S3ObjectStorage(bucket=BUCKET, s3_client=stub_client)

    with storage.open_multipart(key="audio/u/b/book.mp3", content_type="audio/mpeg") as writer:
        writer.write(b"data")

    parts = stub_client.complete_multipart_upload.call_args.kwargs["MultipartUpload"]["Parts"]
    assert parts == [{"ETag": '"etag"', "PartNumber": 1}]
