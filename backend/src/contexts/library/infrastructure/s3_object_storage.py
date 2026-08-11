"""``ObjectStorage`` adapter backed by plain S3 ``PutObject``/``GetObject``
(PLANS/phase-4.md §3/§6.5), plus (phase 5) a bounded-memory multipart writer
for the stitched ``book.mp3``. One instance per bucket, mirroring
``S3PdfStorage``'s shape -- ``interface/dependencies.py`` constructs one for
``audio_bucket`` and one for ``marks_bucket``. Satisfies the Protocol
structurally -- no inheritance.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from src.infrastructure.aws import client
from src.shared_kernel.domain.errors import NotFoundError

# S3's hard floor for every part except the last. A book smaller than this
# uploads as a single (perfectly legal) final part.
MIN_PART_BYTES = 5 * 1024 * 1024

# botocore >= 1.36 defaults `request_checksum_calculation` to "when_supported",
# so it attaches a CRC32 checksum to every UploadPart whether we ask or not.
# If CreateMultipartUpload did NOT declare an algorithm, the upload's checksum
# type is null and the part's is crc32 -- S3 rejects the mismatch with
# "InvalidRequest: Checksum Type mismatch". Declaring it explicitly on all
# three calls keeps them consistent, and is the correct modern multipart flow
# regardless. (Caught by `make smoke` against LocalStack; moto happens to be
# lenient here, so no unit test would have found it.)
CHECKSUM_ALGORITHM = "CRC32"
_CHECKSUM_FIELD = "ChecksumCRC32"


class S3MultipartWriter:
    """``MultipartWriter`` adapter (PLANS/phase-5.md §7.4). Peak resident
    bytes are ``MIN_PART_BYTES + one segment`` (~6 MB) regardless of book
    size."""

    def __init__(self, *, client: Any, bucket: str, key: str, content_type: str) -> None:
        self._client = client
        self._bucket = bucket
        self._key = key
        self._content_type = content_type
        self._buffer = bytearray()
        self._parts: list[dict] = []
        self._upload_id: str | None = None
        self._bytes_written = 0

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    def __enter__(self) -> S3MultipartWriter:
        response = self._client.create_multipart_upload(
            Bucket=self._bucket,
            Key=self._key,
            ContentType=self._content_type,
            ChecksumAlgorithm=CHECKSUM_ALGORITHM,
        )
        self._upload_id = response["UploadId"]
        return self

    def write(self, data: bytes) -> None:
        self._buffer.extend(data)
        self._bytes_written += len(data)
        while len(self._buffer) >= MIN_PART_BYTES:
            self._upload_part(bytes(self._buffer[:MIN_PART_BYTES]))
            del self._buffer[:MIN_PART_BYTES]

    def _upload_part(self, data: bytes) -> None:
        part_number = len(self._parts) + 1
        response = self._client.upload_part(
            Bucket=self._bucket,
            Key=self._key,
            PartNumber=part_number,
            UploadId=self._upload_id,
            Body=data,
            ChecksumAlgorithm=CHECKSUM_ALGORITHM,
        )
        part: dict = {"ETag": response["ETag"], "PartNumber": part_number}
        # CompleteMultipartUpload must echo back each part's checksum when the
        # upload declares an algorithm. `.get` because not every S3-compatible
        # backend returns one, and a missing checksum must not crash the
        # stitch.
        checksum = response.get(_CHECKSUM_FIELD)
        if checksum is not None:
            part[_CHECKSUM_FIELD] = checksum
        self._parts.append(part)

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            # Abort rather than complete: a half-written book.mp3 at a key
            # the book row is about to advertise is much worse than no
            # object at all, and orphaned parts accrue storage cost forever.
            self._abort()
            return None
        try:
            # Flush the tail. `not self._parts` covers the degenerate
            # "nothing written" case -- CompleteMultipartUpload rejects an
            # empty part list.
            if self._buffer or not self._parts:
                self._upload_part(bytes(self._buffer))
                self._buffer.clear()
            self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=self._key,
                UploadId=self._upload_id,
                MultipartUpload={"Parts": self._parts},
            )
        except Exception:
            self._abort()
            raise
        return None

    def _abort(self) -> None:
        self._client.abort_multipart_upload(
            Bucket=self._bucket, Key=self._key, UploadId=self._upload_id
        )


class S3ObjectStorage:
    def __init__(self, *, bucket: str, s3_client: Any | None = None) -> None:
        self._bucket = bucket
        self._client = s3_client if s3_client is not None else client("s3")

    def put_bytes(self, *, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)

    def get_bytes(self, *, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                raise NotFoundError(f"No object at key: {key}") from exc
            raise
        return response["Body"].read()

    def open_multipart(self, *, key: str, content_type: str) -> S3MultipartWriter:
        return S3MultipartWriter(
            client=self._client, bucket=self._bucket, key=key, content_type=content_type
        )
