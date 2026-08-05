from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

from src.auth.local_dev import LocalAuthMiddleware, should_enable


# --- should_enable -----------------------------------------------------------


def test_should_enable_local_is_true() -> None:
    assert should_enable("local") is True


@pytest.mark.parametrize("environment", ["prod", "dev", "pr-7"])
def test_should_enable_non_local_is_false(environment: str) -> None:
    assert should_enable(environment) is False


# --- LocalAuthMiddleware -----------------------------------------------------


def _fake_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.fakesignature"


class _CaptureApp:
    """A tiny fake downstream ASGI app that records the scope it receives."""

    def __init__(self) -> None:
        self.captured_scope: dict | None = None

    async def __call__(self, scope, receive, send):
        self.captured_scope = scope


async def _run(middleware: LocalAuthMiddleware, scope: dict) -> None:
    async def receive():
        return {"type": "http.request"}

    async def send(message):
        pass

    await middleware(scope, receive, send)


def _run_sync(middleware: LocalAuthMiddleware, scope: dict) -> None:
    """Run ``_run`` to completion without disturbing the process-wide
    "current event loop" the way ``asyncio.run()`` does (it explicitly resets
    it to ``None`` on exit) -- that reset breaks Mangum elsewhere in the
    suite, which still calls the deprecated ``asyncio.get_event_loop()``."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run(middleware, scope))
    finally:
        loop.close()


def _http_scope(headers: list[tuple[bytes, bytes]]) -> dict:
    return {"type": "http", "method": "GET", "path": "/me", "headers": headers}


def test_middleware_injects_claims_for_valid_unsigned_jwt() -> None:
    capture = _CaptureApp()
    middleware = LocalAuthMiddleware(capture)
    claims = {"sub": "abc-123", "cognito:username": "dev"}
    token = _fake_jwt(claims)
    scope = _http_scope([(b"authorization", f"Bearer {token}".encode())])

    _run_sync(middleware, scope)

    assert capture.captured_scope is not None
    injected = capture.captured_scope["aws.event"]["requestContext"]["authorizer"]["jwt"][
        "claims"
    ]
    assert injected == claims


def test_middleware_ignores_expired_token() -> None:
    capture = _CaptureApp()
    middleware = LocalAuthMiddleware(capture)
    token = _fake_jwt({"sub": "abc-123", "exp": int(time.time()) - 3600})
    scope = _http_scope([(b"authorization", f"Bearer {token}".encode())])

    _run_sync(middleware, scope)

    assert "aws.event" not in capture.captured_scope


def test_middleware_ignores_garbage_token() -> None:
    capture = _CaptureApp()
    middleware = LocalAuthMiddleware(capture)
    scope = _http_scope([(b"authorization", b"Bearer not-a-jwt-at-all")])

    _run_sync(middleware, scope)

    assert "aws.event" not in capture.captured_scope


def test_middleware_passes_through_absent_header() -> None:
    capture = _CaptureApp()
    middleware = LocalAuthMiddleware(capture)
    scope = _http_scope([])

    _run_sync(middleware, scope)

    assert "aws.event" not in capture.captured_scope


def test_middleware_passes_through_non_http_scope() -> None:
    capture = _CaptureApp()
    middleware = LocalAuthMiddleware(capture)
    scope = {"type": "lifespan"}

    _run_sync(middleware, scope)

    assert capture.captured_scope == {"type": "lifespan"}
