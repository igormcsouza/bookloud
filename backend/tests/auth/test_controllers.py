from __future__ import annotations


def test_me_authed_returns_200(authed_client) -> None:
    response = authed_client.get("/me")
    assert response.status_code == 200
    body = response.json()
    assert body["sub"] == "11111111-2222-3333-4444-555555555555"
    assert body["username"] == "reader"
    assert body["email"] == "reader@example.com"


def test_me_anonymous_returns_401(client) -> None:
    response = client.get("/me")
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


def test_health_still_public(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
