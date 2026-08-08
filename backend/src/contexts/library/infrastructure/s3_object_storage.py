"""``ObjectStorage`` adapter backed by plain S3 ``PutObject``/``GetObject``
(PLANS/phase-4.md §3/§6.5). One instance per bucket, mirroring
``S3PdfStorage``'s shape -- ``interface/dependencies.py`` constructs one for
``audio_bucket`` and one for ``marks_bucket``. Satisfies the Protocol
structurally -- no inheritance.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from src.infrastructure.aws import client
from src.shared_kernel.domain.errors import NotFoundError


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
