from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    """TestClient over the same ``app`` object used by local dev and Lambda."""
    from src.main import app

    with TestClient(app) as test_client:
        yield test_client


CLAIMS = {
    "sub": "11111111-2222-3333-4444-555555555555",
    "cognito:username": "reader",
    "email": "reader@example.com",
}


class WithGatewayClaims:
    """ASGI wrapper simulating the API Gateway JWT authorizer.

    In AWS the authorizer validates the token and forwards its claims in the
    Lambda event; Mangum exposes that event as ``scope["aws.event"]``. Tests
    reproduce exactly that so the app's auth dependency runs for real.
    """

    def __init__(self, app, claims):
        self.app = app
        self.claims = claims

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {
                **scope,
                "aws.event": {
                    "requestContext": {"authorizer": {"jwt": {"claims": self.claims}}}
                },
            }
        await self.app(scope, receive, send)


@pytest.fixture
def authed_client() -> Iterator[TestClient]:
    """TestClient over ``app`` wrapped so it looks like a request that
    already passed API Gateway's Cognito JWT authorizer."""
    from src.main import app

    with TestClient(WithGatewayClaims(app, CLAIMS)) as test_client:
        yield test_client


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every Settings-related env var so a test starts from defaults."""
    for name in (
        "ENVIRONMENT",
        "GIT_SHA",
        "TABLE_NAME",
        "PDF_BUCKET",
        "AUDIO_BUCKET",
        "MARKS_BUCKET",
        "EXTRACT_QUEUE_URL",
        "LOG_LEVEL",
        "AWS_ENDPOINT_URL",
    ):
        monkeypatch.delenv(name, raising=False)
