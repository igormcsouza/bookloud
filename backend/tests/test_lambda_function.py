from __future__ import annotations

import json

from lambda_function import handler


def _api_gateway_v2_event(method: str, path: str) -> dict:
    """A minimal synthetic API Gateway v2 (HTTP API) proxy event."""
    return {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"host": "example.execute-api.us-east-1.amazonaws.com"},
        "requestContext": {
            "http": {
                "method": method,
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
            },
            "requestId": "test-request-id",
            "routeKey": f"{method} {path}",
            "stage": "$default",
        },
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
