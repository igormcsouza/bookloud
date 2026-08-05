from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.auth.dependencies import CurrentUser, claims_from_request, get_current_user


def _request(scope_extra: dict | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/me",
        "headers": [],
        **(scope_extra or {}),
    }
    return Request(scope)


# --- claims_from_request ---------------------------------------------------


def test_claims_from_request_no_aws_event() -> None:
    assert claims_from_request(_request()) is None


def test_claims_from_request_empty_request_context() -> None:
    request = _request({"aws.event": {}})
    assert claims_from_request(request) is None


def test_claims_from_request_empty_authorizer() -> None:
    request = _request({"aws.event": {"requestContext": {}}})
    assert claims_from_request(request) is None

    request = _request({"aws.event": {"requestContext": {"authorizer": {}}}})
    assert claims_from_request(request) is None


def test_claims_from_request_populated_claims() -> None:
    claims = {"sub": "abc", "cognito:username": "reader"}
    request = _request(
        {"aws.event": {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}}
    )
    assert claims_from_request(request) == claims


# --- get_current_user -------------------------------------------------------


def test_get_current_user_no_claims_raises_401() -> None:
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(_request(), None)
    assert exc_info.value.status_code == 401


def test_get_current_user_claims_without_sub_raises_401() -> None:
    claims = {"cognito:username": "reader"}
    request = _request(
        {"aws.event": {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}}
    )
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(request, None)
    assert exc_info.value.status_code == 401


def test_get_current_user_id_token_shape() -> None:
    claims = {"sub": "abc-123", "cognito:username": "reader", "email": "r@example.com"}
    request = _request(
        {"aws.event": {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}}
    )
    user = get_current_user(request, None)
    assert user == CurrentUser(
        sub="abc-123", username="reader", email="r@example.com", claims=claims
    )


def test_get_current_user_access_token_shape_falls_back_to_username() -> None:
    claims = {"sub": "abc-123", "username": "reader"}
    request = _request(
        {"aws.event": {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}}
    )
    user = get_current_user(request, None)
    assert user.username == "reader"


def test_get_current_user_missing_username_falls_back_to_sub() -> None:
    claims = {"sub": "abc-123"}
    request = _request(
        {"aws.event": {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}}
    )
    user = get_current_user(request, None)
    assert user.username == "abc-123"
    assert user.email is None
