from __future__ import annotations

import json

from lambda_function import handler


def _api_gateway_v2_event(method: str, path: str, claims: dict | None = None) -> dict:
    """A minimal synthetic API Gateway v2 (HTTP API) proxy event.

    ``claims`` (when given) injects ``requestContext.authorizer.jwt.claims``,
    exactly what the Cognito JWT authorizer forwards for an authenticated
    request; omitted, the event looks like an anonymous request that never
    should have reached the Lambda (were the authorizer wired for real).
    """
    request_context = {
        "http": {
            "method": method,
            "path": path,
            "protocol": "HTTP/1.1",
            "sourceIp": "127.0.0.1",
        },
        "requestId": "test-request-id",
        "routeKey": f"{method} {path}",
        "stage": "$default",
    }
    if claims is not None:
        request_context["authorizer"] = {"jwt": {"claims": claims}}

    return {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"host": "example.execute-api.us-east-1.amazonaws.com"},
        "requestContext": request_context,
        "isBase64Encoded": False,
    }


class _Context:
    function_name = "bookloud-api"
    memory_limit_in_mb = 512
    invoked_function_arn = (
        "arn:aws:lambda:us-east-1:123456789012:function:bookloud-api"
    )
    aws_request_id = "test-request-id"


def test_handler_health_route() -> None:
    event = _api_gateway_v2_event("GET", "/health")
    response = handler(event, _Context())

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["status"] == "ok"
    assert body["service"] == "bookloud-api"


def test_handler_unknown_route_returns_404() -> None:
    event = _api_gateway_v2_event("GET", "/does-not-exist")
    response = handler(event, _Context())

    assert response["statusCode"] == 404


def test_handler_me_with_claims_returns_200() -> None:
    """The Mangum harness check that scope["aws.event"] plumbing survives
    the real adapter: a request that already passed the Cognito JWT
    authorizer (claims present) reaches /me successfully."""
    claims = {"sub": "abc-123", "cognito:username": "reader"}
    event = _api_gateway_v2_event("GET", "/me", claims=claims)
    response = handler(event, _Context())

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["sub"] == "abc-123"
    assert body["username"] == "reader"


def test_handler_me_without_claims_returns_401() -> None:
    event = _api_gateway_v2_event("GET", "/me")
    response = handler(event, _Context())

    assert response["statusCode"] == 401
