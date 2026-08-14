import pytest
from fastapi.testclient import TestClient

from src.chat_app import app

@pytest.fixture
def client():
    return TestClient(app)

def test_chat_app_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

def test_chat_app_auth_required_for_chat(client):
    response = client.post("/books/b1/chat", json={"question": "hi"})
    assert response.status_code == 401
    assert response.json()["message"] == "UNAUTHENTICATED"
