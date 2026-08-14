from datetime import datetime, timezone

import pytest

from src.contexts.library.application.chat import (
    AskBookQuestion,
    ClearBookChat,
    ListBookChat,
)
from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chat import ChatMessage, ChatRole
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.value_objects import BookStatus, ChunkStatus
from src.shared_kernel.domain.errors import ConflictError, NotFoundError, QuotaExceededError


class FakeBookRepo:
    def __init__(self, books):
        self.books = books

    def get(self, user_id, book_id):
        b = self.books.get(book_id)
        if b and b.user_id == user_id:
            return b
        return None

class FakeChunkRepo:
    def list_for_book(self, book_id):
        return [Chunk(book_id, 0, "u1", "text", 0, 4, None, None, ChunkStatus.DONE, 1, 1)]

class FakeChatRepo:
    def __init__(self):
        self.msgs = []
        self.cleared = False
        
    def list_messages(self, book_id, limit=50):
        return self.msgs
        
    def clear(self, book_id):
        self.cleared = True
        self.msgs = []

class FakeQuotaRepo:
    def __init__(self, should_pass=True):
        self.should_pass = should_pass
        
    def increment_and_check(self, user_id, date, limit):
        return self.should_pass

class FakeIdGen:
    def __init__(self):
        self.i = 0
    def generate(self):
        self.i += 1
        return f"id{self.i}"

class FakeClock:
    def now(self):
        return datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)

@pytest.fixture
def base_deps():
    now_str = datetime.now(timezone.utc).isoformat()
    book = Book("b1", "u1", "Title", BookStatus.READY, 1, 1, 1, now_str, now_str)
    foreign_book = Book("b2", "u2", "Title", BookStatus.READY, 1, 1, 1, now_str, now_str)
    no_text_book = Book("b3", "u1", "Title", BookStatus.UPLOADED, 0, 0, 0, now_str, now_str)
    
    return {
        "book_repository": FakeBookRepo({"b1": book, "b2": foreign_book, "b3": no_text_book}),
        "chunk_repository": FakeChunkRepo(),
        "chat_repository": FakeChatRepo(),
        "chat_quota_repository": FakeQuotaRepo(),
        "id_generator": FakeIdGen(),
        "clock": FakeClock(),
        "daily_limit": 50,
    }


def test_ask_foreign_book(base_deps):
    uc = AskBookQuestion(**base_deps)
    with pytest.raises(NotFoundError):
        uc.execute("u1", "b2", "q", 0)

def test_list_foreign_book(base_deps):
    uc = ListBookChat(base_deps["book_repository"], base_deps["chat_repository"])
    with pytest.raises(NotFoundError):
        uc.execute("u1", "b2")

def test_clear_foreign_book(base_deps):
    uc = ClearBookChat(base_deps["book_repository"], base_deps["chat_repository"])
    with pytest.raises(NotFoundError):
        uc.execute("u1", "b2")

def test_ask_no_text(base_deps):
    uc = AskBookQuestion(**base_deps)
    with pytest.raises(ConflictError):
        uc.execute("u1", "b3", "q", 0)

def test_list_no_text(base_deps):
    uc = ListBookChat(base_deps["book_repository"], base_deps["chat_repository"])
    with pytest.raises(ConflictError):
        uc.execute("u1", "b3")

def test_ask_quota_exceeded(base_deps):
    base_deps["chat_quota_repository"] = FakeQuotaRepo(should_pass=False)
    uc = AskBookQuestion(**base_deps)
    with pytest.raises(QuotaExceededError):
        uc.execute("u1", "b1", "q", 0)

def test_ask_success_ordering(base_deps):
    uc = AskBookQuestion(**base_deps)
    result = uc.execute("u1", "b1", "q", 0)
    
    assert result.user_message.role == ChatRole.USER
    assert result.assistant_message.role == ChatRole.ASSISTANT
    
    # Check that assistant message timestamp > user message timestamp for SK sorting
    assert result.assistant_message.created_at > result.user_message.created_at

def test_list_success(base_deps):
    base_deps["chat_repository"].msgs = [
        ChatMessage("b1", "1", "u1", ChatRole.USER, "q", 0, "2026"),
    ]
    uc = ListBookChat(base_deps["book_repository"], base_deps["chat_repository"])
    res = uc.execute("u1", "b1")
    assert len(res) == 1

def test_clear_success(base_deps):
    uc = ClearBookChat(base_deps["book_repository"], base_deps["chat_repository"])
    uc.execute("u1", "b1")
    assert base_deps["chat_repository"].cleared is True
