"""``PdfStorage`` adapter backed by S3 presigned POSTs (PLANS/phase-3.md §4/
§9.4). Satisfies the Protocol structurally -- no inheritance.
"""

from __future__ import annotations

from typing import Any

import boto3
from botocore.exceptions import ClientError

from src.config import settings
from src.contexts.library.domain.storage import DEFAULT_EXPIRES_IN, MAX_UPLOAD_BYTES, PresignedUpload
from src.shared_kernel.domain.errors import NotFoundError

_CONTENT_TYPE = "application/pdf"


def _build_client() -> Any:
    # PLANS/phase-3.md §9.4: generate_presigned_post signs URLs against the
    # client's own endpoint, so inside docker-compose that would produce
    # http://localstack:4566/... -- unreachable from the browser/host.
    # s3_public_endpoint_url is the host-reachable override for local dev;
    # falls back to aws_endpoint_url (also unset in real AWS, where boto3
    # resolves the real regional endpoint).
    endpoint = settings.s3_public_endpoint_url or settings.aws_endpoint_url or None
    kwargs: dict[str, Any] = {}
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)


class S3PdfStorage:
    def __init__(
        self,
        *,
        bucket: str,
        expires_in: int = DEFAULT_EXPIRES_IN,
        max_bytes: int = MAX_UPLOAD_BYTES,
        client: Any | None = None,
    ) -> None:
        self._bucket = bucket
        self._expires_in = expires_in
        self._max_bytes = max_bytes
        self._client = client if client is not None else _build_client()

    def presigned_upload(self, *, key: str) -> PresignedUpload:
        # The key is pinned exactly (no `${filename}`, no `starts-with`
        # condition) -- see infrastructure/s3_keys.py's module docstring for
        # why that's what makes cross-user writes impossible even though the
        # POST itself has no auth of its own.
        response = self._client.generate_presigned_post(
            Bucket=self._bucket,
            Key=key,
            Fields={"Content-Type": _CONTENT_TYPE},
            Conditions=[
                {"Content-Type": _CONTENT_TYPE},
                ["content-length-range", 0, self._max_bytes],
            ],
            ExpiresIn=self._expires_in,
        )
        return PresignedUpload(
            url=response["url"],
            fields=response["fields"],
            key=key,
            expires_in=self._expires_in,
            max_bytes=self._max_bytes,
        )

    def get_bytes(self, *, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                raise NotFoundError(f"No object at key: {key}") from exc
            raise
        return response["Body"].read()
