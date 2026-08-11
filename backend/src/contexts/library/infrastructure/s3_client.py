"""The one S3 client whose endpoint is reachable **from the browser**
(PLANS/phase-6.md §4.2 -- promoted verbatim from ``s3_pdf_storage``'s
private ``_build_client``).

Promoted because the presigned *download* has exactly the same failure mode
as the presigned *upload*, and it fails silently: the URL is well-formed,
the signature is valid, and the browser simply cannot connect. Keeping two
copies of that rule would be one copy too many.
"""

from __future__ import annotations

from typing import Any

import boto3
from botocore.config import Config

from src.config import settings

# PLANS/phase-6.md §3.3 reasons entirely in SigV4 terms ("SignedHeaders for a
# presigned GET is just host", so `Range` is unsigned) and §12.2/§13.2 assert
# `X-Amz-Signature` in the minted URL -- but botocore's *default* for an S3
# presigned GET in us-east-1 is still the legacy **SigV2** query string
# (`AWSAccessKeyId=...&Signature=...`), which produces none of those
# parameters. SigV2 also happens not to sign `Range`, so seeking would have
# worked either way; what would not have worked is AWS's own deprecation --
# SigV2 is unsupported in every region launched after 2014. Pinned rather
# than inherited so the presigned download matches its own documentation.
#
# Applied ONLY where it is asked for: `S3PdfStorage`'s presigned POST is a
# deployed, working path and is deliberately left on whatever botocore
# defaults to, since changing its policy fields is a risk this phase has no
# reason to take.
SIGNATURE_VERSION_V4 = "s3v4"


def public_s3_client(*, signature_version: str | None = None) -> Any:
    """An S3 client whose endpoint is reachable **from the browser**.

    boto3 signs presigned URLs against the client's own endpoint, so inside
    docker-compose ``src/infrastructure/aws.py``'s ``client("s3")`` would
    mint ``http://localstack:4566/...`` -- a hostname that resolves only on
    the compose network. ``settings.s3_public_endpoint_url``
    (``http://localhost:4566``) is the host-reachable override; it falls back
    to ``aws_endpoint_url``, and to ``None`` in real AWS where boto3 resolves
    the real regional endpoint. PLANS/phase-3.md §9.4, and now
    PLANS/phase-6.md §4.2.

    Deliberately **not** lazy, unlike ``sqs_synthesis_queue.py``'s ``_sqs``:
    S3 has a global endpoint, so constructing this with no region and no
    credentials does not raise. (That adapter's docstring states the
    converse for SQS, which is exactly why *it* had to be lazy.)
    ``test_s3_audio_delivery.py`` keeps a regression test on it anyway --
    the backend test job runs with no region and no credentials at all.

    ``signature_version`` is opt-in (see ``SIGNATURE_VERSION_V4`` above): the
    presigned *download* asks for SigV4, the presigned *upload* keeps
    botocore's default.
    """
    endpoint = settings.s3_public_endpoint_url or settings.aws_endpoint_url or None
    kwargs: dict[str, Any] = {}
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    if signature_version:
        kwargs["config"] = Config(signature_version=signature_version)
    return boto3.client("s3", **kwargs)
