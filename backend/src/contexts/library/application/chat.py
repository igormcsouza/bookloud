from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

from src.contexts.library.application.use_cases import _load_owned_book
from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chat import (
    ChatMessage,
    ChatRole,
    chat_availability,
    resolve_context,
)
from src.contexts.library.domain.repository import (
    BookRepository,
    ChunkRepository,
    ChatRepository,
    ChatQuotaRepository,
)
from src.shared_kernel.application.ports import IdGenerator, Clock
from src.shared_kernel.domain.errors import ConflictError, QuotaExceededError


class ListBookChat:
    def __init__(self, book_repository: BookRepository, chat_repository: ChatRepository) -> None:
        self.book_repository = book_repository
        self.chat_repository = chat_repository

    def execute(self, user_id: str, book_id: str) -> list[ChatMessage]:
        book = _load_owned_book(self.book_repository, user_id, book_id)
        if not chat_availability(book):
            raise ConflictError("Chat is not available for this book (NO_TEXT)")
        return self.chat_repository.list_messages(book.id)


class ClearBookChat:
    def __init__(self, book_repository: BookRepository, chat_repository: ChatRepository) -> None:
        self.book_repository = book_repository
        self.chat_repository = chat_repository

    def execute(self, user_id: str, book_id: str) -> int:
        book = _load_owned_book(self.book_repository, user_id, book_id)
        return self.chat_repository.clear(book.id)


@dataclass(frozen=True)
class AskBookQuestionResult:
    context: "ChatContext"  # Avoid circular or heavy imports at runtime
    user_message: ChatMessage
    assistant_message: ChatMessage


class AskBookQuestion:
    def __init__(
        self,
        book_repository: BookRepository,
        chunk_repository: ChunkRepository,
        chat_repository: ChatRepository,
        chat_quota_repository: ChatQuotaRepository,
        id_generator: IdGenerator,
        clock: Clock,
        daily_limit: int,
    ) -> None:
        self.book_repository = book_repository
        self.chunk_repository = chunk_repository
        self.chat_repository = chat_repository
        self.chat_quota_repository = chat_quota_repository
        self.id_generator = id_generator
        self.clock = clock
        self.daily_limit = daily_limit

    def execute(
        self, user_id: str, book_id: str, question: str, anchor_chunk: int | None
    ) -> AskBookQuestionResult:
        book = _load_owned_book(self.book_repository, user_id, book_id)
        if not chat_availability(book):
            raise ConflictError("Chat is not available for this book (NO_TEXT)")

        # Quota check
        now = self.clock.now()
        date_str = now.strftime("%Y-%m-%d")
        if not self.chat_quota_repository.increment_and_check(user_id, date_str, self.daily_limit):
            raise QuotaExceededError("Daily chat quota exceeded")

        chunks = self.chunk_repository.list_for_book(book.id)
        history = self.chat_repository.list_messages(book.id)

        context = resolve_context(
            book=book,
            chunks=chunks,
            history=history,
            anchor_chunk=anchor_chunk,
            question=question,
        )

        user_msg = ChatMessage(
            book_id=book.id,
            message_id=self.id_generator.new_id(),
            user_id=user_id,
            role=ChatRole.USER,
            content=context.question,
            anchored_chunk=context.anchor_chunk,
            created_at=now.isoformat(),
        )
        
        # Artificial microsecond delay or identical timestamp, but typically id_generator and suffix in SK prevents collision.
        # We use a distinct message id. Let's make the assistant message have a slightly later timestamp or just a different ID.
        # Using same timestamp is fine because `created_at`+`msg_id` is unique. 
        # But we want to ensure assistant message comes *after* user message in the DB sort.
        # So we advance clock or just rely on UUID suffix for uniqueness, but for chronological sort, they should have exact same or later time.
        # Since `created_at` is part of SK string, let's just use the exact same timestamp. UUIDs are random, so ordering of identical timestamps relies on UUID.
        # Wait, if they have identical timestamps, sorting by SK might put assistant before user if its UUID is alphabetically smaller!
        # Let's add 1 microsecond to the assistant timestamp to guarantee strict chronological order in SK.
        import datetime
        ast_time = now + datetime.timedelta(microseconds=1)

        assistant_msg = ChatMessage(
            book_id=book.id,
            message_id=self.id_generator.new_id(),
            user_id=user_id,
            role=ChatRole.ASSISTANT,
            content="",  # to be populated by stream
            anchored_chunk=context.anchor_chunk,
            created_at=ast_time.isoformat(),
        )

        return AskBookQuestionResult(
            context=context,
            user_message=user_msg,
            assistant_message=assistant_msg,
        )
