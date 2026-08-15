import pytest
from fastapi.testclient import TestClient

from src.chat_app import app
from src.contexts.library.interface.dependencies import get_ask_book_question, get_chat_model
from src.auth.dependencies import get_current_user

class FakeChatModel:
    name = "fake"
    enabled = True
    reason = None
    def stream(self, context):
        from src.contexts.library.domain.chat import ChatDelta
        yield ChatDelta("hello")

class FakeAskUseCase:
    def execute(self, user_id, book_id, question, anchor_chunk):
        from src.contexts.library.domain.chat import ChatContext, ChatMessage, ChatRole, ContextChunk
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        return type("Result", (), {
            "context": ChatContext(
                book_id=book_id, book_title="T", chunks_total=1, anchor_chunk=0,
                chunks=(ContextChunk(0, "txt", 0, 0, True),), history=(), question=question
            ),
            "user_message": ChatMessage(book_id, "u1", user_id, ChatRole.USER, question, 0, now),
            "assistant_message": ChatMessage(book_id, "a1", user_id, ChatRole.ASSISTANT, "", 0, now),
        })()

class FakeChatRepo:
    def save_turn(self, u, a):
        pass

@pytest.fixture
def client():
    from src.auth.dependencies import CurrentUser
    # Override dependencies for controller test
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(sub="user1", username="user1", email=None)
    app.dependency_overrides[get_ask_book_question] = lambda: FakeAskUseCase()
    app.dependency_overrides[get_chat_model] = lambda: FakeChatModel()
    
    from src.contexts.library.interface.dependencies import get_chat_repository
    app.dependency_overrides[get_chat_repository] = lambda: FakeChatRepo()
    
    # We must also bypass the FunctionUrlAuthMiddleware for this controller test, or provide a valid aws.event in scope
    # Wait, TestClient sends HTTP which triggers the middleware.
    # The middleware expects a token. We can mock get_verifier.
    
    with TestClient(app) as c:
        yield c
        
    app.dependency_overrides.clear()

def test_ask_question_controller(client, monkeypatch):
    import src.auth.function_url as fu
    monkeypatch.setattr(fu, "get_verifier", lambda: type("V", (), {"verify": lambda t, token_use=None: {"sub": "user1", "token_use": "id"}})())
    
    response = client.post(
        "/books/b1/chat",
        headers={"Authorization": "Bearer valid"},
        json={"question": "What?", "anchoredChunk": 0}
    )
    
    assert response.status_code == 200
    assert "event: meta" in response.text
    assert "event: delta\ndata: {\"text\": \"hello\"}" in response.text
    assert "event: done" in response.text
