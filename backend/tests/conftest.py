from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    """TestClient over the same ``app`` object used by local dev and Lambda."""
    from src.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every Settings-related env var so a test starts from defaults."""
    for name in (
        "ENVIRONMENT",
        "GIT_SHA",
        "TABLE_NAME",
        "PDF_BUCKET",
        "AUDIO_BUCKET",
        "MARKS_BUCKET",
        "EXTRACT_QUEUE_URL",
        "LOG_LEVEL",
        "AWS_ENDPOINT_URL",
    ):
        monkeypatch.delenv(name, raising=False)
