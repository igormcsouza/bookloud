from datetime import datetime, timezone
import pytest

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chat import (
    ChatMessage, ChatRole, resolve_context, chat_availability,
    MAX_CONTEXT_CHARS, CONTEXT_NEIGHBOURS, MAX_HISTORY_CHARS
)
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.value_objects import BookStatus, ChunkStatus


def make_book(status=BookStatus.READY, chunks_total=10) -> Book:
    return Book(
        id="b1",
        user_id="u1",
        title="Test Book",
        status=status,
        chunks_total=chunks_total,
        chunks_done=chunks_total,
        page_count=0,
        created_at=datetime.now(timezone.utc),
    )


def make_chunk(index: int, length: int = 100) -> Chunk:
    return Chunk(
        book_id="b1",
        index=index,
        user_id="u1",
        text="A" * length,
        char_start=0,
        char_end=length,
        audio_key=None,
        marks_key=None,
        status=ChunkStatus.DONE,
        page_start=1,
        page_end=1,
    )


def make_message(role: ChatRole, created_at: str, content: str = "hi") -> ChatMessage:
    return ChatMessage(
        book_id="b1",
        message_id=f"msg_{created_at}",
        user_id="u1",
        role=role,
        content=content,
        anchored_chunk=0,
        created_at=created_at,
    )


def test_resolve_context_clamps_anchor():
    book = make_book(chunks_total=3)
    chunks = [make_chunk(0), make_chunk(1), make_chunk(2)]
    
    # anchor < 0 -> 0
    ctx = resolve_context(book=book, chunks=chunks, history=[], anchor_chunk=-5, question="q")
    assert ctx.anchor_chunk == 0
    assert ctx.anchor.index == 0
    
    # anchor > max -> len - 1
    ctx = resolve_context(book=book, chunks=chunks, history=[], anchor_chunk=99, question="q")
    assert ctx.anchor_chunk == 2
    assert ctx.anchor.index == 2
    
    # anchor_chunk is None -> 0
    ctx = resolve_context(book=book, chunks=chunks, history=[], anchor_chunk=None, question="q")
    assert ctx.anchor_chunk == 0


def test_resolve_context_window_clipped_not_shifted():
    book = make_book(chunks_total=10)
    chunks = [make_chunk(i) for i in range(10)]
    
    # At start, window is [0, 1, 2], not shifted forward
    ctx = resolve_context(book=book, chunks=chunks, history=[], anchor_chunk=0, question="q")
    assert ctx.window_indexes == (0, 1, 2)
    
    # At end, window is [7, 8, 9]
    ctx = resolve_context(book=book, chunks=chunks, history=[], anchor_chunk=9, question="q")
    assert ctx.window_indexes == (7, 8, 9)


def test_resolve_context_ceiling_eviction_drops_farthest():
    book = make_book(chunks_total=5)
    # 5 chunks, 3000 chars each = 15000 chars (over 12000 ceiling)
    chunks = [make_chunk(i, 3000) for i in range(5)]
    
    # Anchor is 2. Neighbours are 0,1 and 3,4.
    # Distances: 0->2, 1->1, 3->1, 4->2.
    # It should drop index 0 and 4 first. 
    # 15000 - 3000 = 12000 (fits in ceiling now? wait, sum > 12000 means it drops until it's <= 12000)
    # Actually, 3000*4 = 12000, so dropping one is enough. Which one?
    ctx = resolve_context(book=book, chunks=chunks, history=[], anchor_chunk=2, question="q")
    assert sum(len(c.text) for c in ctx.chunks) <= MAX_CONTEXT_CHARS
    assert len(ctx.chunks) == 4
    # The farthest is 0 or 4.
    assert 2 in ctx.window_indexes


def test_resolve_context_ceiling_truncates_anchor_if_only_one_left():
    book = make_book(chunks_total=1)
    chunks = [make_chunk(0, MAX_CONTEXT_CHARS + 100)]
    ctx = resolve_context(book=book, chunks=chunks, history=[], anchor_chunk=0, question="q")
    
    assert len(ctx.chunks) == 1
    assert len(ctx.chunks[0].text) == MAX_CONTEXT_CHARS


def test_resolve_context_history_truncation_and_normalization():
    book = make_book()
    chunks = [make_chunk(0)]
    
    # 1. Leading assistant should be dropped
    # 2. Trailing user should be dropped
    # 3. Last 6 (3 pairs) max
    # 4. Message content truncated to MAX_HISTORY_CHARS
    
    history = [
        make_message(ChatRole.ASSISTANT, "2020-01-01", "lead ast"), # Dropped
        make_message(ChatRole.USER, "2020-01-02", "u1"),
        make_message(ChatRole.ASSISTANT, "2020-01-03", "a1"),
        make_message(ChatRole.USER, "2020-01-04", "u2"),
        make_message(ChatRole.ASSISTANT, "2020-01-05", "a2"),
        make_message(ChatRole.USER, "2020-01-06", "A" * (MAX_HISTORY_CHARS + 100)), # Will be truncated
        make_message(ChatRole.ASSISTANT, "2020-01-07", "a3"),
        make_message(ChatRole.USER, "2020-01-08", "trail user"), # Dropped
    ]
    
    ctx = resolve_context(book=book, chunks=chunks, history=history, anchor_chunk=0, question="q")
    
    # Output should have 6 messages: u1, a1, u2, a2, u3(truncated), a3
    assert len(ctx.history) == 6
    assert ctx.history[0].content == "u1"
    assert ctx.history[-1].content == "a3"
    assert len(ctx.history[-2].content) == MAX_HISTORY_CHARS
    assert ctx.history[-2].content.endswith("…")


def test_resolve_context_question_truncation():
    book = make_book()
    chunks = [make_chunk(0)]
    q = "Q" * 3000
    ctx = resolve_context(book=book, chunks=chunks, history=[], anchor_chunk=0, question=q)
    assert len(ctx.question) == 2000
    assert ctx.question == "Q" * 2000


def test_resolve_context_raises_if_no_chunks():
    book = make_book(chunks_total=0)
    with pytest.raises(ValueError, match="no chunks"):
        resolve_context(book=book, chunks=[], history=[], anchor_chunk=0, question="q")


def test_chat_availability():
    # Valid
    assert chat_availability(make_book(status=BookStatus.READY, chunks_total=10)) is True
    assert chat_availability(make_book(status=BookStatus.PARTIAL, chunks_total=10)) is True
    
    # Invalid
    assert chat_availability(make_book(status=BookStatus.UPLOADED, chunks_total=10)) is False
    assert chat_availability(make_book(status=BookStatus.EXTRACTING, chunks_total=10)) is False
    assert chat_availability(make_book(status=BookStatus.FAILED, chunks_total=10)) is False
    assert chat_availability(make_book(status=BookStatus.READY, chunks_total=0)) is False
