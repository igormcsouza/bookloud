"""Bookloud backend — FastAPI app.

The same ``app`` object is used by local dev (uvicorn), the tests, and the
Lambda handler (``lambda_function.py``, via Mangum). Configuration is
entirely env-var driven (``src/config.py``). Domain routes live in their own
packages and are included here; ``library/``, ``reading/``, ``chat/`` land in
phases 2-7.
"""

from __future__ import annotations

import logging

from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.auth.controllers import router as auth_router
from src.auth.local_dev import LocalAuthMiddleware, should_enable
from src.config import settings
from src.health.controllers import router as health_router
from src.shared_kernel.domain.errors import DomainError

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("bookloud")

app = FastAPI(title="Bookloud API")

# Permissive CORS: the frontend is served from a CloudFront domain that
# differs per environment (dev/pr-N/prod), so allow any origin rather than
# hard-coding one.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(auth_router)

if should_enable(settings.environment):
    app.add_middleware(LocalAuthMiddleware)


@app.exception_handler(ClientError)
async def aws_error_handler(request: Request, exc: ClientError) -> JSONResponse:
    """Translate storage/AWS (boto3) failures into a 503 instead of a raw 500.

    Applies to every route: any AWS error raised by a repository/gateway —
    table missing, throttling, credentials — is logged with its stack trace
    and reported to the client without leaking AWS details.
    """
    logger.error("AWS error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=503, content={"detail": "Storage temporarily unavailable"}
    )


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    """Map domain errors to their declared HTTP status."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})
