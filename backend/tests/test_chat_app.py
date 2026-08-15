import asyncio
import importlib

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from src.chat_app import app, aws_error_handler

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

    # Restore local state for every test after this one in the session --
    # must happen with ENVIRONMENT actually unset, not merely "about to be
    # unset once monkeypatch tears down after this function returns". The
    # module reload itself is synchronous and reads os.environ right now, so
    # reloading while monkeypatch's patch is still active leaves the module
    # stuck on pr-0 for the rest of the session.
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    importlib.reload(config_module)
    importlib.reload(chat_app_module)


def test_cors_middleware_present_only_in_local(monkeypatch):
    """§4.2's deliberate asymmetry: everywhere except local, the Function
    URL's own native CORS config (api_stack.py's FunctionUrlCorsOptions)
    already answers preflight without invoking the function and stamps
    every real response. Mounting CORSMiddleware there too means BOTH
    layers write an Access-Control-Allow-Origin header on a real POST
    response (preflight OPTIONS is answered by the platform before the
    Lambda ever runs, so it never showed there) -- browsers reject a
    response with more than one value for that header outright, so every
    deployed chat request failed with "Failed to fetch" despite the Lambda
    completing successfully server-side. Caught live against a real deploy,
    not by any test -- which is why this one exists now."""
    from starlette.middleware.cors import CORSMiddleware

    import src.config as config_module
    import src.chat_app as chat_app_module

    def middleware_classes(app):
        return [m.cls for m in app.user_middleware]

    # local (the suite's default): CORSMiddleware present -- there is no
    # Function URL in front of uvicorn locally, so nothing else answers CORS.
    assert CORSMiddleware in middleware_classes(chat_app_module.app)

    # any deployed environment: CORSMiddleware absent.
    monkeypatch.setenv("ENVIRONMENT", "pr-0")
    importlib.reload(config_module)
    importlib.reload(chat_app_module)
    assert CORSMiddleware not in middleware_classes(chat_app_module.app)

    monkeypatch.delenv("ENVIRONMENT", raising=False)
    importlib.reload(config_module)
    importlib.reload(chat_app_module)


def test_client_error_maps_to_503_llm_unavailable():
    """PLANS/phase-7.md §4.5 step 7: get_chat_model()'s eager get_secret()
    call raises ClientError as a FastAPI dependency -- before any byte of
    the response -- and this handler is what turns that into one clean 503,
    never a raw ResourceNotFoundException and never a half stream."""
    exc = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
        "GetSecretValue",
    )
    fake_request = type(
        "FakeRequest", (), {"method": "POST", "url": type("U", (), {"path": "/books/b1/chat"})()}
    )()

    response = asyncio.run(aws_error_handler(fake_request, exc))

    assert response.status_code == 503
    body = response.body.decode()
    assert "LLM_UNAVAILABLE" in body
    assert "ResourceNotFoundException" not in body
