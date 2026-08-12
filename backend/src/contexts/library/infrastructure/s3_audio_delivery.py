"""``AudioDelivery`` adapter backed by an S3 presigned GET
(PLANS/phase-6.md §3/§4.2). Satisfies the Protocol structurally -- no
inheritance, matching ``S3PdfStorage``/``S3ObjectStorage``.

Two facts this adapter rests on, both stated because neither is obvious:

1. **``Range`` is unsigned.** SigV4 query-string presigning signs the
   method, the host, the path and the query parameters; ``SignedHeaders``
   for a presigned GET is just ``host``. S3 therefore honours whatever
   ``Range`` the browser sends, which is the *only* reason ``<audio>``
   seeking works at all -- Chrome issues ``Range: bytes=N-`` on every seek
   and expects a ``206``. ``local/smoke_test.py`` asserts this against a
   real S3 API, because no unit test, mock or fake can prove it.
2. **The client must be ``public_s3_client()``**, never
   ``src/infrastructure/aws.py``'s ``client("s3")``. The latter would sign
   against ``http://localstack:4566`` inside compose and mint a URL no
   browser can reach -- well-formed, correctly signed, and unfetchable.
"""

from __future__ import annotations

from typing import Any

from src.contexts.library.domain.storage import AUDIO_URL_EXPIRES_IN, PresignedDownload
from src.contexts.library.infrastructure.s3_client import SIGNATURE_VERSION_V4, public_s3_client


class S3AudioDelivery:
    def __init__(self, *, bucket: str, client: Any | None = None) -> None:
        self._bucket = bucket
        # SigV4 explicitly: botocore's default for an S3 presigned GET is
        # still the legacy SigV2 query string, which AWS no longer supports
        # in any region launched after 2014 -- see s3_client.py's
        # SIGNATURE_VERSION_V4 comment.
        self._client = (
            client
            if client is not None
            else public_s3_client(signature_version=SIGNATURE_VERSION_V4)
        )

    def presigned_download(
        self, *, key: str, expires_in: int = AUDIO_URL_EXPIRES_IN
    ) -> PresignedDownload:
        # Purely local signing -- no network call, so this costs nothing and
        # cannot fail on a missing object. A key that does not exist yields a
        # perfectly valid URL that 404s when fetched; callers guard on the
        # DynamoDB row's audioKey being set instead (application/delivery.py).
        url = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )
        return PresignedDownload(url=url, expires_in=expires_in)
