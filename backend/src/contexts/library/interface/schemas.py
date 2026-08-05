"""``Book``/``Chunk`` -> camelCase response dicts, matching both the
DynamoDB attribute names and the TypeScript frontend (§10 Q7)."""

from __future__ import annotations

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk


def book_to_dict(book: Book) -> dict:
    return {
        "id": book.id,
        "title": book.title,
        "status": book.status.value,
        "chunksTotal": book.chunks_total,
        "chunksDone": book.chunks_done,
        "pageCount": book.page_count,
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
    }
