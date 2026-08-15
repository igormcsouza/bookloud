import importlib

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

def test_chat_app_auth_required_for_chat(monkeypatch):
    # ENVIRONMENT defaults to "local" for the whole suite, which mounts
    # LocalAuthMiddleware (falls through to get_current_user's {"detail": ...}
    # 401 instead of rejecting up front). This is the one test that must
    # exercise the deployed-environment path -- FunctionUrlAuthMiddleware,
    # {"message": "UNAUTHENTICATED"} -- so it rebuilds the app under a
    # non-local ENVIRONMENT. Reloading src.config only rebinds its module
    # attribute; every other test module already holds its own `settings`
    # reference from collection time, so this is isolated.
    monkeypatch.setenv("ENVIRONMENT", "pr-0")
    import src.config as config_module
    importlib.reload(config_module)
    import src.chat_app as chat_app_module
    importlib.reload(chat_app_module)

    client = TestClient(chat_app_module.app)
    response = client.post("/books/b1/chat", json={"question": "hi"})
    assert response.status_code == 401
    assert response.json()["message"] == "UNAUTHENTICATED"

    importlib.reload(config_module)
    importlib.reload(chat_app_module)
