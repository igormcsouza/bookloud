from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from src.contexts.library.infrastructure import secrets
from src.contexts.library.infrastructure.secrets import _clear_cache_for_tests, get_secret


@pytest.fixture(autouse=True)
def _reset_cache():
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


@pytest.fixture
def secretsmanager(monkeypatch: pytest.MonkeyPatch):
    import src.config as config

    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.setattr(config.settings, "aws_endpoint_url", "")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    with mock_aws():
        client = boto3.client("secretsmanager", region_name="us-east-1")
        client.create_secret(Name="bookloud/google-tts-api-key", SecretString="super-secret-api-key")
        yield client


def test_get_secret_returns_secret_string(secretsmanager) -> None:
    assert get_secret("bookloud/google-tts-api-key") == "super-secret-api-key"


def test_get_secret_caches_after_first_fetch(secretsmanager) -> None:
    first = get_secret("bookloud/google-tts-api-key")
    # Delete the secret from the backing store -- if get_secret hit the
    # network again it would raise; the cache must make this a no-op fetch.
    secretsmanager.delete_secret(SecretId="bookloud/google-tts-api-key", ForceDeleteWithoutRecovery=True)
    second = get_secret("bookloud/google-tts-api-key")
    assert first == second == "super-secret-api-key"


def test_get_secret_different_names_cached_independently(secretsmanager) -> None:
    secretsmanager.create_secret(Name="other/secret", SecretString="other-value")
    assert get_secret("bookloud/google-tts-api-key") == "super-secret-api-key"
    assert get_secret("other/secret") == "other-value"


def test_clear_cache_for_tests_actually_clears(secretsmanager) -> None:
    get_secret("bookloud/google-tts-api-key")
    assert secrets._cache
    _clear_cache_for_tests()
    assert secrets._cache == {}
