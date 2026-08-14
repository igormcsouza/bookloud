from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _restore_asyncio_event_loop() -> Iterator[None]:
    """``asyncio.run()`` (used synchronously by ``EdgeTtsSynthesizer`` --
    PLANS/phase-4.md §7.2) calls ``set_event_loop(None)`` on exit, which
    breaks Mangum's still-deprecated ``asyncio.get_event_loop()`` call for
    every *later* test in the same process (it stops auto-creating a loop
    once one has ever been explicitly unset). Restore a fresh event loop
    after every test so this doesn't leak across test order -- this is the
    generalized fix for the one-off comment already in pyproject.toml's
    filterwarnings about Mangum's event-loop usage."""
    yield
    try:
        asyncio.set_event_loop(asyncio.new_event_loop())
    except RuntimeError:
        pass


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
        "SYNTHESIZE_QUEUE_URL",
        "STITCH_QUEUE_URL",
        "EDGE_TTS_VOICE",
        "GOOGLE_TTS_VOICE",
        "GOOGLE_TTS_SECRET_NAME",
        "SYNTHESIZE_MAX_RECEIVE_COUNT",
        "STITCH_MAX_RECEIVE_COUNT",
        "SYNTHESIS_STUB_MODE",
        "LOG_LEVEL",
        "AWS_ENDPOINT_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
