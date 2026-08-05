"""Claims extraction and the ``get_current_user`` dependency.

The backend **never validates a token**. API Gateway's Cognito JWT authorizer
does that before the Lambda is invoked, and forwards the verified claims in
``requestContext.authorizer.jwt.claims``; Mangum surfaces the whole Lambda
event as ``request.scope["aws.event"]``. ``_bearer_scheme`` exists purely so
``/docs`` renders the Authorize button; ``auto_error=False`` so it can never
itself 401.

The 401 raised in ``get_current_user`` is a **defence-in-depth** path — in a
correctly deployed environment API Gateway already returned 401 and the
Lambda was never invoked. It is the real enforcement point only in local dev
(see ``src/auth/local_dev.py``).

The id token (not the access token) is what API Gateway's authorizer accepts
here (``jwtConfiguration.audience = [clientId]`` matches the id token's
``aud`` claim; access tokens carry ``client_id`` instead and are rejected).
Consequence: the username claim is ``cognito:username``, not ``username``
(an access token would use the latter) — the dependency below reads
``cognito:username`` with a ``username`` fallback so it keeps working if
this ever changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    sub: str  # canonical user id -> phase 2's USER#<id> partition key
    username: str
    email: str | None
    claims: dict = field(default_factory=dict)


def claims_from_request(request: Request) -> dict | None:
    """Extract Cognito JWT claims from the API Gateway event, if present."""
    event = request.scope.get("aws.event") or {}
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    return (authorizer.get("jwt") or {}).get("claims")


def get_current_user(
    request: Request,
    _credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> CurrentUser:
    claims = claims_from_request(request)
    if not claims:
        raise HTTPException(401, "Not authenticated")
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(401, "Invalid token claims")
    return CurrentUser(
        sub=sub,
        username=claims.get("cognito:username") or claims.get("username") or sub,
        email=claims.get("email"),
        claims=claims,
    )
