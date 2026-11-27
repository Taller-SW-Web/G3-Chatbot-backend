"""Integration tests against Kong OSS DB-less gateway (ADR-0031, H5-G2).

Skipped when GATEWAY_TEST_URL is unset so local runs without Kong stay green.
In CI, the `gateway-integration` job boots FastAPI + Kong in Docker and sets
GATEWAY_TEST_URL=http://127.0.0.1:8080.
"""
from __future__ import annotations

import os

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def gateway_url() -> str:
    url = os.getenv("GATEWAY_TEST_URL")
    if not url:
        pytest.skip("Falta GATEWAY_TEST_URL: integración del gateway Kong no ejecutada")
    return url.rstrip("/")


async def test_health_routes_through_kong_with_trace_headers(gateway_url: str) -> None:
    async with httpx.AsyncClient(base_url=gateway_url, timeout=15.0) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.headers.get("x-gateway") == "kong"
    assert response.headers.get("x-request-id")
    payload = response.json()
    assert payload.get("status") in {"ok", "degraded"}


async def test_api_v1_catalog_routes_through_kong(gateway_url: str) -> None:
    async with httpx.AsyncClient(base_url=gateway_url, timeout=15.0) as client:
        response = await client.get("/api/v1/catalogo/productos")

    assert response.status_code == 200
    assert response.headers.get("x-gateway") == "kong"
    payload = response.json()
    assert isinstance(payload, dict)


async def test_undeclared_route_is_rejected_by_kong(gateway_url: str) -> None:
    async with httpx.AsyncClient(base_url=gateway_url, timeout=15.0) as client:
        response = await client.get("/ruta-no-declarada-h5-g2")

    assert response.status_code == 404
    assert "no Route matched" in response.text


async def test_sse_chat_streams_through_kong_preserving_chat_sid_cookie(
    gateway_url: str,
) -> None:
    async with httpx.AsyncClient(base_url=gateway_url, timeout=30.0) as client:
        created = await client.post("/api/v1/chat/conversaciones", json={})
        assert created.status_code == 201
        assert created.headers.get("x-gateway") == "kong"
        assert "chat_sid" in client.cookies

        conv_id = created.json()["conversacionId"]

        async with client.stream(
            "POST",
            f"/api/v1/chat/conversaciones/{conv_id}/mensajes",
            json={"texto": "Hola"},
        ) as stream_resp:
            assert stream_resp.status_code == 200
            assert stream_resp.headers.get("x-gateway") == "kong"
            assert stream_resp.headers.get("content-type", "").startswith("text/event-stream")
            body = (await stream_resp.aread()).decode("utf-8")

    assert "event: aceptado" in body
    assert "event: fin" in body


async def test_upload_route_allows_over_1mb_while_api_rejects_oversized_json(
    gateway_url: str,
) -> None:
    oversized_json = b'{"x":"' + (b"a" * (1100 * 1024)) + b'"}'
    payload_over_1mb = b"x" * (1100 * 1024)

    async with httpx.AsyncClient(base_url=gateway_url, timeout=30.0) as client:
        # General /api/v1 route enforces 1 MB request-size-limiting.
        rejected = await client.post(
            "/api/v1/chat/conversaciones",
            content=oversized_json,
            headers={"content-type": "application/json"},
        )
        assert rejected.status_code == 413
        assert rejected.headers.get("x-gateway") == "kong"

        # Upload route (/adjuntos) allows up to 6 MB: Kong forwards it to FastAPI
        # instead of rejecting with 413.
        created = await client.post("/api/v1/chat/conversaciones", json={})
        assert created.status_code == 201
        conv_id = created.json()["conversacionId"]

        upload_resp = await client.post(
            f"/api/v1/chat/conversaciones/{conv_id}/adjuntos",
            files={"archivo": ("prueba.bin", payload_over_1mb, "application/octet-stream")},
        )
        assert upload_resp.status_code != 413
        assert upload_resp.headers.get("x-gateway") == "kong"
