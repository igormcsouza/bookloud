"""boto3 client factory honouring ``AWS_ENDPOINT_URL`` (LocalStack support).

Every context's repository/gateway should build its boto3 clients through
``client()`` rather than calling ``boto3.client()`` directly, so local dev
(docker-compose against LocalStack) and real AWS both work with zero code
changes — only the ``AWS_ENDPOINT_URL`` env var differs.
"""

from __future__ import annotations

from typing import Any

import boto3

from src.config import settings


def client(service: str) -> Any:
    """Return a boto3 client for ``service``, pointed at LocalStack when
    ``settings.aws_endpoint_url`` is set (local dev), or real AWS otherwise."""
    kwargs: dict[str, Any] = {}
    if settings.aws_endpoint_url:
        kwargs["endpoint_url"] = settings.aws_endpoint_url
    return boto3.client(service, **kwargs)


def resource(service: str) -> Any:
    """Same as :func:`client` but for a boto3 resource (e.g. DynamoDB Table)."""
    kwargs: dict[str, Any] = {}
    if settings.aws_endpoint_url:
        kwargs["endpoint_url"] = settings.aws_endpoint_url
    return boto3.resource(service, **kwargs)
