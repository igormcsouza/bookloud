import pytest
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.auth.function_url import FunctionUrlAuthMiddleware
from src.auth.jwt_verifier import InvalidToken
import src.auth.function_url as function_url_module


async def protected_route(request):
    claims = request.scope.get("aws.event", {}).get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims")
    return JSONResponse({"claims": claims})

async def health_route(request):
    return JSONResponse({"status": "ok"})


@pytest.fixture
def app():
    app = Starlette(routes=[
        Route("/protected", protected_route),
        Route("/health", health_route),
    ])
    app.add_middleware(FunctionUrlAuthMiddleware)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class DummyVerifier:
    def verify(self, token):
        if token == "valid_token":
            return {"sub": "user_123", "token_use": "id"}
        raise InvalidToken("Bad token")


@pytest.fixture(autouse=True)
def mock_verifier(monkeypatch):
    monkeypatch.setattr(function_url_module, "get_verifier", lambda: DummyVerifier())


def test_missing_auth_header(client):
    response = client.get("/protected")
    assert response.status_code == 401
    assert response.json()["message"] == "UNAUTHENTICATED"


def test_invalid_auth_header_format(client):
    response = client.get("/protected", headers={"Authorization": "NotBearer valid_token"})
    assert response.status_code == 401
    assert response.json()["message"] == "UNAUTHENTICATED"


def test_invalid_token(client):
    response = client.get("/protected", headers={"Authorization": "Bearer invalid_token"})
    assert response.status_code == 401
    assert response.json()["message"] == "UNAUTHENTICATED"


def test_valid_token_injects_claims(client):
    response = client.get("/protected", headers={"Authorization": "Bearer valid_token"})
    assert response.status_code == 200
    assert response.json()["claims"]["sub"] == "user_123"


def test_health_exempt(client):
    # No auth header, should still pass
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
