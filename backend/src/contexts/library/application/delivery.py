"""Read-side delivery use cases (PLANS/phase-6.md §4) -- what the reader UI
needs to actually *play* and *follow* a book, as opposed to merely list it.

Four use cases, two shapes:

- ``GetBookAudioUrl``/``GetChunkAudioUrl`` mint a presigned S3 GET, because
  the ``<audio>`` element's request cannot carry a ``Authorization`` header
  and the bytes cannot fit through Lambda's 6 MB response cap (§3.1/§3.2).
- ``GetBookManifest``/``GetChunkMarks`` return the stored S3 object
  **verbatim**, because the documents are already the contract
  (PLANS/phase-5.md §7.2 calls the manifest "a contract" in so many words)
  and re-shaping them in the API would create a second place for the schema
  to drift.

Every one of them reaches the book through ``_load_owned_book``, so another
user's book is a **404, never a 403** -- the repo's standing rule since
phase 2.

Why a missing *audio* is a ``ConflictError`` (409) but a missing *manifest*
is a ``NotFoundError`` (404): "this book has no audio" is a legitimate,
documented terminal state (``PARTIAL``/``NO_AUDIO`` -- the everyday state of
every environment except local compose, PLANS/phase-4.md §0), so the client
needs to distinguish it from "no such book". A missing manifest, by
contrast, genuinely is a missing document; §9's degradation table renders it
as "text only, no audio" rather than as a crash.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.contexts.library.application.use_cases import _load_owned_book
from src.contexts.library.domain.repository import BookRepository, ChunkRepository
from src.contexts.library.domain.storage import (
    AUDIO_URL_EXPIRES_IN,
    AudioDelivery,
    ObjectStorage,
    PresignedDownload,
)
from src.shared_kernel.domain.errors import ConflictError, NotFoundError

AUDIO_CONTENT_TYPE = "audio/mpeg"


@dataclass(frozen=True)
class AudioUrl:
    download: PresignedDownload
    duration_ms: int
    content_type: str = AUDIO_CONTENT_TYPE


class GetBookAudioUrl:
    """``GET /books/{id}/audio``. Mints a presigned S3 GET for the stitched
    ``book.mp3``."""

    def __init__(self, book_repository: BookRepository, audio_delivery: AudioDelivery) -> None:
        self._book_repository = book_repository
        self._audio_delivery = audio_delivery

    def execute(
        self, user_id: str, book_id: str, *, expires_in: int = AUDIO_URL_EXPIRES_IN
    ) -> AudioUrl:
        book = _load_owned_book(self._book_repository, user_id, book_id)
        if book.audio_key is None:
            raise ConflictError("Book has no audio")
        download = self._audio_delivery.presigned_download(
            key=book.audio_key, expires_in=expires_in
        )
        return AudioUrl(download=download, duration_ms=book.audio_duration_ms)


class GetChunkAudioUrl:
    """``GET /books/{id}/chunks/{n}/audio``. Ships **backend-only** this
    phase (PLANS/phase-6.md OQ-1): it is what makes phase-5 §7.2's invariant
    3 -- "a book whose concatenation failed is still fully playable from
    per-chunk artefacts" -- a real capability rather than a claim. The second
    playback engine that would consume it in the UI is deferred until prod
    actually produces a ``STITCH_FAILED`` book."""

    def __init__(
        self,
        book_repository: BookRepository,
        chunk_repository: ChunkRepository,
        audio_delivery: AudioDelivery,
    ) -> None:
        self._book_repository = book_repository
        self._chunk_repository = chunk_repository
        self._audio_delivery = audio_delivery

    def execute(
        self, user_id: str, book_id: str, index: int, *, expires_in: int = AUDIO_URL_EXPIRES_IN
    ) -> AudioUrl:
        _load_owned_book(self._book_repository, user_id, book_id)
        chunk = self._chunk_repository.get(book_id, index)
        if chunk is None:
            raise NotFoundError("Chunk not found")
        if chunk.audio_key is None:
            raise ConflictError("Chunk has no audio")
        download = self._audio_delivery.presigned_download(
            key=chunk.audio_key, expires_in=expires_in
        )
        return AudioUrl(download=download, duration_ms=chunk.duration_ms)


class GetBookManifest:
    """``GET /books/{id}/manifest`` -- the ``marks/<u>/<b>/book.json`` bytes,
    proxied rather than presigned (PLANS/phase-6.md Q3). ~60 KB, nowhere near
    the 6 MB cap, and proxying buys three things a presign would not: no CORS
    rule on ``marks_bucket`` (it has none today and gains none), ownership
    enforced by ``_load_owned_book`` rather than by key opacity, and exactly
    **one** exception to "everything uses ``authFetch``" instead of three."""

    def __init__(self, book_repository: BookRepository, marks_storage: ObjectStorage) -> None:
        self._book_repository = book_repository
        self._marks_storage = marks_storage

    def execute(self, user_id: str, book_id: str) -> bytes:
        book = _load_owned_book(self._book_repository, user_id, book_id)
        if book.manifest_key is None:
            raise NotFoundError("Book has no manifest")
        # `manifest_key` set but the object gone: a PR bucket torn down under
        # a still-open tab. S3ObjectStorage.get_bytes already raises
        # NotFoundError, and §9's degradation table turns that into "text
        # only, no audio" rather than a crash -- so it is deliberately NOT
        # caught and re-raised as something louder.
        return self._marks_storage.get_bytes(key=book.manifest_key)


class GetChunkMarks:
    """``GET /books/{id}/chunks/{n}/marks``. A 404 here is the **normal
    case** for every chunk in a PR environment, which is why the frontend's
    ``MarksCache`` negatively caches it (``lib/marks.ts``): without that,
    the rAF loop would re-request a missing marks document 60 times a second
    from a UI that merely looks stuck."""

    def __init__(
        self,
        book_repository: BookRepository,
        chunk_repository: ChunkRepository,
        marks_storage: ObjectStorage,
    ) -> None:
        self._book_repository = book_repository
        self._chunk_repository = chunk_repository
        self._marks_storage = marks_storage

    def execute(self, user_id: str, book_id: str, index: int) -> bytes:
        _load_owned_book(self._book_repository, user_id, book_id)
        chunk = self._chunk_repository.get(book_id, index)
        if chunk is None:
            raise NotFoundError("Chunk not found")
        if chunk.marks_key is None:
            raise NotFoundError("Chunk has no marks")
        return self._marks_storage.get_bytes(key=chunk.marks_key)
