from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterator, Protocol, Sequence

from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.shared_kernel.domain.errors import ValidationError


CONTEXT_NEIGHBOURS = 2        # anchor +/- 2  -> up to 5 chunks
MAX_CONTEXT_CHARS = 12_000    # hard ceiling on the assembled window
MAX_HISTORY_PAIRS = 3         # 3 user/assistant pairs -> 6 messages
MAX_HISTORY_CHARS = 1_000     # per message, tail-truncated
MAX_QUESTION_CHARS = 2_000


class ChatRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class FinishReason(str, Enum):
    END_TURN = "END_TURN"
    MAX_TOKENS = "MAX_TOKENS"
    REFUSAL = "REFUSAL"
    DISABLED = "DISABLED"
    ERROR = "ERROR"
    TRUNCATED = "TRUNCATED"


class ChatDisabledReason(str, Enum):
    NON_PROD = "NON_PROD"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass
class ChatMessage:
    book_id: str
    message_id: str
    user_id: str
    role: ChatRole
    content: str
    anchored_chunk: int
    created_at: str
    position_ms: int | None = None
    model: str | None = None
    finish_reason: FinishReason | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


@dataclass(frozen=True)
class ContextChunk:
    index: int
    text: str
    page_start: int
    page_end: int
    is_anchor: bool


@dataclass(frozen=True)
class ChatContext:
    book_id: str
    book_title: str
    chunks_total: int
    anchor_chunk: int
    chunks: tuple[ContextChunk, ...]
    history: tuple[ChatMessage, ...]
    question: str

    @property
    def anchor(self) -> ContextChunk:
        for c in self.chunks:
            if c.is_anchor:
                return c
        raise RuntimeError("ChatContext has no anchor chunk")

    @property
    def window_indexes(self) -> tuple[int, ...]:
        return tuple(c.index for c in self.chunks)


@dataclass(frozen=True)
class ChatDelta:
    text: str


class ChatModel(Protocol):
    name: str

    def stream(self, context: ChatContext) -> Iterator[ChatDelta]: ...  # pragma: no cover


def chat_availability(book: Book) -> bool:
    """Returns True if the book has extracted text to ground chat answers.
    PLANS/phase-7.md §4.5 says chat requires chunks_total > 0.
    """
    from src.contexts.library.domain.value_objects import BookStatus
    
    if book.chunks_total == 0:
        return False
    if book.status in (BookStatus.UPLOADED, BookStatus.EXTRACTING, BookStatus.FAILED):
        return False
    return True


def resolve_context(
    *,
    book: Book,
    chunks: Sequence[Chunk],
    history: Sequence[ChatMessage],
    anchor_chunk: int | None,
    question: str,
) -> ChatContext:
    """PLANS/phase-7.md §6.1. Pure and total: no I/O, and it raises only for
    a book with zero chunks.
    """
    if not chunks or book.chunks_total == 0:
        raise ValueError("Cannot resolve context for a book with no chunks")

    # 1. Clamp anchor
    clamped_anchor = min(max(anchor_chunk or 0, 0), len(chunks) - 1)

    # 2. Window (clipped, not shifted)
    start_idx = max(0, clamped_anchor - CONTEXT_NEIGHBOURS)
    end_idx = min(len(chunks), clamped_anchor + CONTEXT_NEIGHBOURS + 1)
    window = list(chunks[start_idx:end_idx])

    # 3. Ceiling: drop farthest neighbours first
    while sum(len(c.text) for c in window) > MAX_CONTEXT_CHARS and len(window) > 1:
        # Farthest from anchor
        distances = [(abs(c.index - clamped_anchor), i) for i, c in enumerate(window)]
        # Sort by distance descending, then by index descending (to drop symmetric equally far, drop later first? wait, symmetric drop farthest)
        distances.sort(key=lambda x: (x[0], x[1]), reverse=True)
        idx_to_drop = distances[0][1]
        
        # Don't drop anchor unless it's the only one left
        if window[idx_to_drop].index == clamped_anchor:
            break
        window.pop(idx_to_drop)

    # If anchor alone exceeds ceiling
    if sum(len(c.text) for c in window) > MAX_CONTEXT_CHARS:
        for i, c in enumerate(window):
            if c.index == clamped_anchor:
                # Truncate anchor text
                window[i] = Chunk(
                    book_id=c.book_id,
                    index=c.index,
                    user_id=c.user_id,
                    text=c.text[:MAX_CONTEXT_CHARS],
                    char_start=c.char_start,
                    char_end=c.char_start + MAX_CONTEXT_CHARS, # rough
                    audio_key=c.audio_key,
                    marks_key=c.marks_key,
                    status=c.status,
                    page_start=c.page_start,
                    page_end=c.page_end
                )

    context_chunks = tuple(
        ContextChunk(
            index=c.index,
            text=c.text,
            page_start=c.page_start,
            page_end=c.page_end,
            is_anchor=(c.index == clamped_anchor),
        )
        for c in window
    )

    # 4. History: last 6 messages
    # Sort by created_at ascending
    sorted_history = sorted(history, key=lambda m: m.created_at)
    
    # Take last 6
    recent_history = sorted_history[-MAX_HISTORY_PAIRS * 2:]
    
    # Truncate content
    truncated_history = []
    for m in recent_history:
        content = m.content
        if len(content) > MAX_HISTORY_CHARS:
            content = content[:MAX_HISTORY_CHARS - 1] + "…"
        truncated_history.append(
            ChatMessage(
                book_id=m.book_id,
                message_id=m.message_id,
                user_id=m.user_id,
                role=m.role,
                content=content,
                anchored_chunk=m.anchored_chunk,
                created_at=m.created_at,
                position_ms=m.position_ms,
                model=m.model,
                finish_reason=m.finish_reason,
                input_tokens=m.input_tokens,
                output_tokens=m.output_tokens,
                cached_input_tokens=m.cached_input_tokens,
            )
        )

    # Normalise: drop leading assistant
    while truncated_history and truncated_history[0].role == ChatRole.ASSISTANT:
        truncated_history.pop(0)

    # Normalise: drop trailing user if no assistant after it
    while truncated_history and truncated_history[-1].role == ChatRole.USER:
        truncated_history.pop()

    # 5. Question
    final_question = question[:MAX_QUESTION_CHARS]

    return ChatContext(
        book_id=book.id,
        book_title=book.title,
        chunks_total=book.chunks_total,
        anchor_chunk=clamped_anchor,
        chunks=context_chunks,
        history=tuple(truncated_history),
        question=final_question,
    )
