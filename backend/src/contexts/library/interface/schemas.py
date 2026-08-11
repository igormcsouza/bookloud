"""``Book``/``Chunk``/``PresignedUpload`` -> camelCase response dicts,
matching both the DynamoDB attribute names and the TypeScript frontend (§10
Q7). ``source_key`` is deliberately never exposed here -- it's an internal
storage detail; ``upload_to_dict``'s ``key`` already tells the client what it
needs for the one presigned request that follows book creation."""

from __future__ import annotations

from src.contexts.library.application.delivery import AudioUrl
from src.contexts.library.application.resynthesis import ResynthesizeBookResult
from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.storage import PresignedUpload
from src.contexts.library.domain.value_objects import TERMINAL_BOOK_STATUSES


def book_to_dict(book: Book) -> dict:
    return {
        "id": book.id,
        "title": book.title,
        "status": book.status.value,
        "chunksTotal": book.chunks_total,
        "chunksDone": book.chunks_done,
        "chunksFailed": book.chunks_failed,
        "pageCount": book.page_count,
        "failureReason": book.failure_reason,
        # Phase 5's stitch outputs, mirrored here so the book payload and the
        # status payload can never disagree about the artefacts. These are S3
        # keys, not URLs, deliberately: turning one into something an
        # <audio> element can play (presigned GET vs CloudFront origin, Range
        # support, expiry) is a phase-6 decision with phase-6 constraints
        # (PLANS/phase-5.md OQ-3).
        "audioKey": book.audio_key,
        "manifestKey": book.manifest_key,
        "audioDurationMs": book.audio_duration_ms,
        "createdAt": book.created_at,
        "updatedAt": book.updated_at,
    }


def book_status_to_dict(book: Book) -> dict:
    """``GET /books/{id}/status`` (PLANS/phase-5.md §8) -- deliberately a
    separate function, not a filter over ``book_to_dict``, so the poll
    contract can stay small and stable while the book payload grows in
    phases 6-7.

    It earns its place on exactly two server-computed fields:

    - ``terminal``, over ``TERMINAL_BOOK_STATUSES``. Without it a client's
      stop condition is ``status == "READY"``, which never fires for a
      ``PARTIAL`` book -- i.e. for *every* book in local dev and every PR
      environment (§3.1). Putting the closed set on the server means the
      phase-6 poll loop, the smoke test and any future client agree by
      construction rather than by three independent copies of a status list.
    - ``progress.percent``, so one place decides what a zero-``chunksTotal``
      book is rather than three clients each dividing by zero differently.
    """
    terminal = book.status in TERMINAL_BOOK_STATUSES
    return {
        "id": book.id,
        "status": book.status.value,
        "terminal": terminal,
        "progress": {
            "chunksTotal": book.chunks_total,
            "chunksDone": book.chunks_done,
            "chunksFailed": book.chunks_failed,
            "percent": _percent(book.chunks_done, book.chunks_total, terminal=terminal),
        },
        "failureReason": book.failure_reason,
        "audio": {
            "audioKey": book.audio_key,
            "manifestKey": book.manifest_key,
            "durationMs": book.audio_duration_ms,
        },
        "updatedAt": book.updated_at,
    }


def _percent(done: int, total: int, *, terminal: bool) -> int:
    if total == 0:
        # A book that failed extraction has chunksTotal == 0 and must not
        # render as a 0%-forever progress bar.
        return 100 if terminal else 0
    return round(100 * done / total)


def chunk_to_dict(chunk: Chunk) -> dict:
    return {
        "index": chunk.index,
        "text": chunk.text,
        "charStart": chunk.char_start,
        "charEnd": chunk.char_end,
        "audioKey": chunk.audio_key,
        "marksKey": chunk.marks_key,
        "status": chunk.status.value,
        "pageStart": chunk.page_start,
        "pageEnd": chunk.page_end,
        "durationMs": chunk.duration_ms,
        "failureReason": chunk.failure_reason,
        "synthesisSource": chunk.synthesis_source,
    }


def audio_url_to_dict(audio: AudioUrl) -> dict:
    """``GET /books/{id}/audio`` and ``GET /books/{id}/chunks/{n}/audio``
    (PLANS/phase-6.md §4.3). ``durationMs`` comes from the DynamoDB row, not
    from the file: for a mixed-engine book the browser's ``audio.duration``
    is an *estimate* (no Xing header -- PLANS/phase-5.md §7.1), and the
    manifest, not the media element, is the timeline."""
    return {
        "url": audio.download.url,
        "expiresIn": audio.download.expires_in,
        "durationMs": audio.duration_ms,
        "contentType": audio.content_type,
    }


def resynthesis_to_dict(result: ResynthesizeBookResult, book: Book) -> dict:
    """``POST /books/{id}/resynthesize`` (PLANS/phase-6.md §5.3). Mirrors
    ``POST /books``'s ``{"book": ..., "upload": ...}`` envelope: the reloaded
    book goes back in ``book_status_to_dict``'s exact shape so the client can
    drop it straight into its poll state and resume, with no extra round trip
    just to learn the book is back at ``EXTRACTED``."""
    return {
        "retriedChunks": result.retried_chunks,
        "republishedStitch": result.republished_stitch,
        "book": book_status_to_dict(book),
    }


def upload_to_dict(upload: PresignedUpload) -> dict:
    return {
        "url": upload.url,
        "fields": upload.fields,
        "key": upload.key,
        "expiresIn": upload.expires_in,
        "maxBytes": upload.max_bytes,
    }
