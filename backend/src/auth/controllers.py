"""``GET /me`` — the phase's concrete protected route.

It is what the frontend calls to confirm a session, what the unit tests
assert 401/200 on, and what ``local/smoke_test.py`` hits to prove the
deployed authorizer rejects anonymous requests.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.auth.dependencies import CurrentUser, get_current_user

router = APIRouter(tags=["auth"])


@router.get("/me")
def me(user: CurrentUser = Depends(get_current_user)) -> dict:
    return {"sub": user.sub, "username": user.username, "email": user.email}
