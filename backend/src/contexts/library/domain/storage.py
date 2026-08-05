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
