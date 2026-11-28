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

    @app.get("/api/v1/catalogo/productos")
    async def products():
        return {"items": []}

    return TestClient(app)


def test_blocks_direct_api_requests_while_exempting_healthcheck() -> None:
    client = _make_app("super-secret-token")

    # Container/Railway healthcheck must stay 200 without X-Gateway-Secret.
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    missing = client.get("/api/v1/catalogo/productos")
    assert missing.status_code == 403
    assert missing.headers["content-type"] == "application/problem+json"
    assert missing.json()["code"] == "GATEWAY_REQUIRED"

    wrong = client.get("/api/v1/catalogo/productos", headers={GATEWAY_SECRET_HEADER: "wrong"})
    assert wrong.status_code == 403
    assert wrong.json()["code"] == "GATEWAY_REQUIRED"

    valid = client.get(
        "/api/v1/catalogo/productos",
        headers={GATEWAY_SECRET_HEADER: "super-secret-token"},
    )
    assert valid.status_code == 200
    assert valid.json() == {"items": []}


def test_handles_non_ascii_secret_without_type_error() -> None:
    client = _make_app("clave-segura-ñ-256")
    res = client.get("/api/v1/catalogo/productos", headers={GATEWAY_SECRET_HEADER: "other"})
    assert res.status_code == 403
    assert res.json()["code"] == "GATEWAY_REQUIRED"


def test_allows_requests_when_gateway_secret_is_not_configured() -> None:
    client = _make_app("")
    res = client.get("/api/v1/catalogo/productos")
    assert res.status_code == 200
    assert res.json() == {"items": []}
