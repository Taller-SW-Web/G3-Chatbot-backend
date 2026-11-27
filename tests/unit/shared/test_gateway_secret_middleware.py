"""Unit tests for GatewaySecretMiddleware (#256)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.shared.infrastructure.http.gateway_guard import (
    GATEWAY_SECRET_HEADER,
    GatewaySecretMiddleware,
)


def _make_app(secret: str) -> TestClient:
    app = FastAPI()
    app.add_middleware(GatewaySecretMiddleware, secret=secret)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return TestClient(app)


def test_blocks_direct_requests_without_or_with_wrong_gateway_secret() -> None:
    client = _make_app("super-secret-token")

    missing = client.get("/health")
    assert missing.status_code == 403
    assert missing.headers["content-type"] == "application/problem+json"
    assert missing.json()["code"] == "GATEWAY_REQUIRED"

    wrong = client.get("/health", headers={GATEWAY_SECRET_HEADER: "wrong"})
    assert wrong.status_code == 403
    assert wrong.json()["code"] == "GATEWAY_REQUIRED"

    valid = client.get("/health", headers={GATEWAY_SECRET_HEADER: "super-secret-token"})
    assert valid.status_code == 200
    assert valid.json() == {"status": "ok"}


def test_allows_requests_when_gateway_secret_is_not_configured() -> None:
    client = _make_app("")
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
