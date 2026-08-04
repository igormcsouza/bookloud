from __future__ import annotations

from fastapi.testclient import TestClient

from src.config import settings


def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200


def test_health_body_shape(client: TestClient) -> None:
    response = client.get("/health")
    body = response.json()
    assert set(body.keys()) == {"status", "service", "environment", "commit"}


def test_health_reflects_settings(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["service"] == "bookloud-api"
    assert body["environment"] == settings.environment
    assert body["commit"] == settings.git_sha


def test_root_also_returns_health(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
