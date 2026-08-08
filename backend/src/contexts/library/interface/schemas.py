"""``Book``/``Chunk``/``PresignedUpload`` -> camelCase response dicts,
matching both the DynamoDB attribute names and the TypeScript frontend (§10
Q7). ``source_key`` is deliberately never exposed here -- it's an internal
storage detail; ``upload_to_dict``'s ``key`` already tells the client what it
needs for the one presigned request that follows book creation."""

from __future__ import annotations

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.storage import PresignedUpload


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
        "createdAt": book.created_at,
        "updatedAt": book.updated_at,
    }


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


def upload_to_dict(upload: PresignedUpload) -> dict:
    return {
        "url": upload.url,
        "fields": upload.fields,
        "key": upload.key,
        "expiresIn": upload.expires_in,
        "maxBytes": upload.max_bytes,
    }
