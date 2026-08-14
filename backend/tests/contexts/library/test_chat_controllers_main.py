import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.auth.dependencies import get_current_user
from src.contexts.library.interface.dependencies import get_list_book_chat, get_clear_book_chat
from src.contexts.library.domain.chat import ChatMessage, ChatRole
from src.shared_kernel.domain.errors import ConflictError


class FakeListUseCase:
    def __init__(self, succeed=True):
        self.succeed = succeed

    def execute(self, user_id, book_id):
        if not self.succeed:
            raise ConflictError("NO_TEXT")
        return [ChatMessage("b1", "1", user_id, ChatRole.USER, "q", 0, "2026")]


class FakeClearUseCase:
    def execute(self, user_id, book_id):
        pass


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: type("User", (), {"sub": "u1"})()
    app.dependency_overrides[get_clear_book_chat] = lambda: FakeClearUseCase()
    
    with TestClient(app) as c:
        yield c
        
    app.dependency_overrides.clear()


def test_list_chat_success(client):
    app.dependency_overrides[get_list_book_chat] = lambda: FakeListUseCase(succeed=True)
    response = client.get("/books/b1/chat")
    assert response.status_code == 200
    data = response.json()
    assert data["meta"]["enabled"] is True
    assert len(data["items"]) == 1
    assert data["items"][0]["content"] == "q"


def test_list_chat_no_text(client):
    app.dependency_overrides[get_list_book_chat] = lambda: FakeListUseCase(succeed=False)
    response = client.get("/books/b1/chat")
    assert response.status_code == 200
    data = response.json()
    assert data["meta"]["enabled"] is False
    assert data["meta"]["reason"] == "NO_TEXT"
    assert len(data["items"]) == 0


def test_clear_chat(client):
    response = client.delete("/books/b1/chat")
    assert response.status_code == 204
