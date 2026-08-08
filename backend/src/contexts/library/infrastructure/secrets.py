"""Cached AWS Secrets Manager lookup for the Google TTS API key (PLANS/
phase-4.md §6.4). Module-level cache so warm Lambda invocations don't
re-hit Secrets Manager on every synthesize call -- the key is immutable for
the lifetime of a running container, and re-fetching it per invocation would
add a network round trip to every single chunk for no benefit.
"""

from __future__ import annotations

from src.infrastructure.aws import client

_cache: dict[str, str] = {}


def get_secret(secret_name: str) -> str:
    """Return the secret's ``SecretString``, fetching it once per cold
    Lambda container (or once per test process) and caching it thereafter."""
    if secret_name in _cache:
        return _cache[secret_name]
    response = client("secretsmanager").get_secret_value(SecretId=secret_name)
    value = response["SecretString"]
    _cache[secret_name] = value
    return value


def _clear_cache_for_tests() -> None:
    """Test-only escape hatch -- the module-level cache is deliberately
    global and otherwise has no way to reset between test cases."""
    _cache.clear()
