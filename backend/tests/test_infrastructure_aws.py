from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import src.infrastructure.aws as aws_module


@pytest.fixture
def no_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aws_module.settings, "aws_endpoint_url", "")


@pytest.fixture
def with_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        aws_module.settings, "aws_endpoint_url", "http://localstack:4566"
    )


def test_client_without_endpoint_url(no_endpoint: None) -> None:
    with patch("boto3.client") as mock_client:
        mock_client.return_value = MagicMock()
        aws_module.client("s3")
        mock_client.assert_called_once_with("s3")


def test_client_with_endpoint_url(with_endpoint: None) -> None:
    with patch("boto3.client") as mock_client:
        mock_client.return_value = MagicMock()
        aws_module.client("dynamodb")
        mock_client.assert_called_once_with(
            "dynamodb", endpoint_url="http://localstack:4566"
        )


def test_resource_without_endpoint_url(no_endpoint: None) -> None:
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value = MagicMock()
        aws_module.resource("dynamodb")
        mock_resource.assert_called_once_with("dynamodb")


def test_resource_with_endpoint_url(with_endpoint: None) -> None:
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value = MagicMock()
        aws_module.resource("dynamodb")
        mock_resource.assert_called_once_with(
            "dynamodb", endpoint_url="http://localstack:4566"
        )
