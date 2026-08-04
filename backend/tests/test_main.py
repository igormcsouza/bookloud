from __future__ import annotations

import asyncio
import json

from botocore.exceptions import ClientError
from starlette.requests import Request

from src.main import app, aws_error_handler, domain_error_handler
from src.shared_kernel.domain.errors import ConflictError, DomainError, NotFoundError


def _fake_request(path: str = "/health", method: str = "GET") -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "app": app,
    }
    return Request(scope)


def test_aws_error_handler_returns_503() -> None:
    exc = ClientError(
        error_response={"Error": {"Code": "ResourceNotFoundException", "Message": "boom"}},
        operation_name="GetItem",
    )
    response = asyncio.run(aws_error_handler(_fake_request(), exc))
    assert response.status_code == 503
    assert json.loads(response.body)["detail"] == "Storage temporarily unavailable"


def test_domain_error_handler_maps_status_code() -> None:
    exc = NotFoundError("book not found")
    response = asyncio.run(domain_error_handler(_fake_request(), exc))
    assert response.status_code == 404
    assert json.loads(response.body)["detail"] == "book not found"


def test_domain_error_default_status_code() -> None:
    exc = DomainError("bad request")
    assert exc.status_code == 400
    assert exc.message == "bad request"


def test_conflict_error_status_code() -> None:
    exc = ConflictError("already exists")
    assert exc.status_code == 409
