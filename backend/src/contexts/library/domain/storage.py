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


@dataclass(frozen=True)
class PresignedUpload:
    url: str
    fields: dict[str, str]
    key: str
    expires_in: int
    max_bytes: int


class PdfStorage(Protocol):
    def presigned_upload(self, *, key: str) -> PresignedUpload: ...  # pragma: no cover

    def get_bytes(self, *, key: str) -> bytes: ...  # pragma: no cover


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
