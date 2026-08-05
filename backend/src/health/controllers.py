"""``GET /health`` — the only route Phase 0 needs.

Returning the commit SHA is what lets ``local/smoke_test.py`` prove a
deployed environment is running *this* PR's/commit's code rather than a
stale deploy.
"""

from __future__ import annotations

from fastapi import APIRouter

from src.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "bookloud-api",
        "environment": settings.environment,
        "commit": settings.git_sha,
    }
