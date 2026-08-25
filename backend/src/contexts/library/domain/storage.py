"""Port for the PDF object storage the browser uploads directly to
(PLANS/phase-3.md §4). ``PdfStorage`` is a ``Protocol`` (jgautocar's
convention, matching ``domain/repository.py``); ``infrastructure/
s3_pdf_storage.py`` is the only adapter, backed by S3 presigned POSTs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# 50 MiB / 15-minute presign expiry (PLANS/phase-3.md §13 OQ-8) -- the single
# source of truth for the upload size cap, shared by the presigned POST's
# content-length-range condition (``infrastructure/s3_pdf_storage.py``) and
# the extractor's own defensive check (``infrastructure/pymupdf_extractor.py``)
# so the two can never drift apart.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
DEFAULT_EXPIRES_IN = 15 * 60

# PLANS/phase-6.md §3.3/OQ-3. One hour, and the client must never treat it as
# a guarantee: a URL presigned with the Lambda role's *temporary* credentials
# is void the moment those credentials expire, whatever ``ExpiresIn`` says.
# The real mechanism is the frontend's reactive refresh on the ``<audio>``
# ``error`` event; this constant only decides how often that path runs, which
# is why a longer window is not a substitute for it.
AUDIO_URL_EXPIRES_IN = 3600


@dataclass(frozen=True)
class PresignedUpload:
    url: str
    fields: dict[str, str]
    key: str
    expires_in: int
    max_bytes: int


@dataclass(frozen=True)
class PresignedDownload:
    """A browser-fetchable, self-authenticating GET (PLANS/phase-6.md §3).

    ``<audio src=...>`` issues a plain browser GET that cannot carry an
    ``Authorization`` header, and ``ApiStack`` guards ``/{proxy+}`` with a
    Cognito authorizer reading exactly that header -- so the media request
    has to authenticate by URL or not at all. Proxying the bytes through the
    API is not an alternative but an impossibility: Lambda's response payload
    cap is 6 MB and a 300-page book is ~240 MB (§3.2).
    """

    url: str
    expires_in: int


class AudioDelivery(Protocol):
    """Read-side delivery port (PLANS/phase-6.md §3/§4.2).

    Deliberately **not** a method on ``ObjectStorage``: that port is the
    *workers'* write side, and its only two consumers (synthesize, stitch)
    must never learn how to mint a browser-fetchable URL.
    """

    def presigned_download(
        self, *, key: str, expires_in: int = AUDIO_URL_EXPIRES_IN
    ) -> PresignedDownload: ...  # pragma: no cover


class PdfStorage(Protocol):
    def presigned_upload(self, *, key: str) -> PresignedUpload: ...  # pragma: no cover

    def get_bytes(self, *, key: str) -> bytes: ...  # pragma: no cover

    def delete(self, *, key: str) -> None: ...  # pragma: no cover


class MultipartWriter(Protocol):
    """Streaming write port (PLANS/phase-5.md §7.4). Buffers until it has at
    least the backend's minimum part size, then flushes -- so peak resident
    bytes are ``O(one part + one segment)``, not ``O(book)``.

    Why this exists rather than a ``bytearray`` + ``put_bytes``: a 300-page
    book is ~330 chunks x ~720 KB ~= 240 MB, and materializing ``bytes(...)``
    for a single ``put_object`` peaks near 480 MB. The failure mode would be
    an OOM **in prod only**, on a big book, in an environment that never runs
    an automated test -- precisely the class of bug this codebase cannot
    catch after the fact, so it is designed out.

    Used as a context manager: normal exit completes the upload, an exception
    aborts it (no orphaned multipart parts silently accruing storage cost).
    """

    def write(self, data: bytes) -> None: ...  # pragma: no cover

    def __enter__(self) -> MultipartWriter: ...  # pragma: no cover

    def __exit__(self, exc_type, exc, tb) -> None: ...  # pragma: no cover

    @property
    def bytes_written(self) -> int: ...  # pragma: no cover


class ObjectStorage(Protocol):
    """Write-side port for ``audio_bucket``/``marks_bucket`` (PLANS/
    phase-4.md §3). One adapter instance per bucket -- ``infrastructure/
    s3_object_storage.py``'s ``S3ObjectStorage`` is constructed once per
    bucket, not parameterized by bucket name per call, mirroring
    ``S3PdfStorage``'s one-adapter-per-bucket shape."""

    def put_bytes(self, *, key: str, data: bytes, content_type: str) -> None: ...  # pragma: no cover

    def get_bytes(self, *, key: str) -> bytes: ...  # pragma: no cover

    def open_multipart(
        self, *, key: str, content_type: str
    ) -> MultipartWriter: ...  # pragma: no cover

    def delete(self, *, key: str) -> None: ...  # pragma: no cover

    def delete_many(self, *, keys: list[str]) -> None: ...  # pragma: no cover
