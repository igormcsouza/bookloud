"""Authorizer emulation for local dev.

Locally the backend is plain uvicorn (Phase 0 chose the ``dev`` Dockerfile
target with hot reload, deliberately *not* cashlytics's RIE + ``apigw-proxy``
pair), so there is no API Gateway and no ``aws.event``. Without something,
every protected route 401s locally from this phase onward.

``LocalAuthMiddleware`` is pure-ASGI middleware, mounted only when
``settings.environment == "local"`` (see ``should_enable`` / ``src/main.py``).
"""

from __future__ import annotations

import base64
import binascii
import json
import time


def should_enable(environment: str) -> bool:
    """``"local"`` is a value the Lambda **never** has (``ApiStack`` always
    sets ``ENVIRONMENT=dev|pr-N|prod``), which is the safety property that
    keeps this middleware out of every real deployment."""
    return environment == "local"


class LocalAuthMiddleware:
    """Decode (WITHOUT verifying) a Bearer token and inject the claims into
    ``scope["aws.event"]``, exactly as API Gateway's JWT authorizer does in
    AWS. Local dev only — see ``should_enable()``. Signature verification is
    API Gateway's job in AWS; here the token comes from cognito-local, whose
    signatures are fake anyway.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        claims = self._claims_from_scope(scope)
        if claims is not None:
            scope = {
                **scope,
                "aws.event": {
                    "requestContext": {"authorizer": {"jwt": {"claims": claims}}}
                },
            }
        await self.app(scope, receive, send)

    @staticmethod
    def _claims_from_scope(scope) -> dict | None:
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        if not auth.startswith("Bearer "):
            return None
        token = auth[len("Bearer ") :].strip()
        return _decode_claims(token)


def _decode_claims(token: str) -> dict | None:
    """Best-effort, unverified decode of a JWT's payload segment.

    Returns ``None`` (meaning: pass the request through untouched) on
    malformed base64/JSON, or an already-expired ``exp`` claim.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp < time.time():
        return None
    return claims
