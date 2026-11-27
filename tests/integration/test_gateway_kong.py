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


async def test_openapi_and_docs_are_published_through_kong(gateway_url: str) -> None:
    async with httpx.AsyncClient(base_url=gateway_url, timeout=15.0) as client:
        openapi_resp = await client.get("/openapi.json")
        docs_resp = await client.get("/docs")
        redoc_resp = await client.get("/redoc")

    assert openapi_resp.status_code == 200
    assert openapi_resp.headers.get("x-gateway") == "kong"
    assert openapi_resp.headers.get("ratelimit-limit") == "60"
    spec = openapi_resp.json()
    assert "openapi" in spec
    assert "/api/v1/catalogo/productos" in spec.get("paths", {})

    assert docs_resp.status_code == 200
    assert docs_resp.headers.get("x-gateway") == "kong"
    assert "swagger" in docs_resp.text.lower()

    assert redoc_resp.status_code == 200
    assert redoc_resp.headers.get("x-gateway") == "kong"
    assert "redoc" in redoc_resp.text.lower()


async def test_rate_limit_headers_and_429_enforced_by_kong(gateway_url: str) -> None:
    async with httpx.AsyncClient(base_url=gateway_url, timeout=15.0) as client:
        catalog_resp = await client.get(
            "/api/v1/catalogo/productos",
            headers={"X-Forwarded-For": "198.51.100.10"},
        )
        assert catalog_resp.status_code == 200
        assert catalog_resp.headers.get("x-gateway") == "kong"
        assert catalog_resp.headers.get("ratelimit-limit") == "120"
        assert catalog_resp.headers.get("ratelimit-remaining") is not None

        # Verify route-specific perimeter limits match OpenAPI routes (H5-G3).
        login_resp = await client.post(
            "/api/v1/sesion/login",
            json={},
            headers={"X-Forwarded-For": "198.51.100.11"},
        )
        assert login_resp.headers.get("x-gateway") == "kong"
        assert login_resp.headers.get("ratelimit-limit") == "15"

        reaction_resp = await client.post(
            "/api/v1/mensajes/00000000-0000-0000-0000-000000000001/reaccion",
            json={},
            headers={"X-Forwarded-For": "198.51.100.12"},
        )
        assert reaction_resp.headers.get("x-gateway") == "kong"
        assert reaction_resp.headers.get("ratelimit-limit") == "35"

        created = await client.post(
            "/api/v1/chat/conversaciones",
            json={},
            headers={"X-Forwarded-For": "198.51.100.13"},
        )
        assert created.status_code == 201
        conv_id = created.json()["conversacionId"]

        sse_resp = await client.post(
            f"/api/v1/chat/conversaciones/{conv_id}/mensajes",
            json={"texto": "Hola"},
            headers={"X-Forwarded-For": "198.51.100.13"},
        )
        assert sse_resp.status_code == 200
        assert sse_resp.headers.get("ratelimit-limit") == "25"

        upload_resp = await client.post(
            f"/api/v1/chat/conversaciones/{conv_id}/adjuntos",
            files={"archivo": ("tiny.bin", b"abc", "application/octet-stream")},
            headers={"X-Forwarded-For": "198.51.100.14"},
        )
        assert upload_resp.headers.get("x-gateway") == "kong"
        assert upload_resp.headers.get("ratelimit-limit") == "15"

        # Rotating spoofed X-Forwarded-For headers must NOT evade the 5/min bucket (#256),
        # because KONG_TRUSTED_IPS restricts trusted proxies to private/Railway CIDRs.
        for idx in range(5):
            attempt = await client.post(
                "/api/v1/sesion/registro",
                json={},
                headers={"X-Forwarded-For": f"198.51.100.{90 + idx}"},
            )
            assert attempt.status_code != 429
            assert attempt.headers.get("ratelimit-limit") == "5"

        blocked = await client.post(
            "/api/v1/sesion/registro",
            json={},
            headers={"X-Forwarded-For": "203.0.113.250"},
        )
        assert blocked.status_code == 429
        assert blocked.headers.get("x-gateway") == "kong"
        assert blocked.headers.get("retry-after") is not None
        assert "API rate limit exceeded" in blocked.text

        # Dedicated test IP for /sugerencias burst (10/hour).
        suggestions_headers = {"X-Forwarded-For": "198.51.100.77"}
        for _ in range(10):
            s_attempt = await client.post(
                "/api/v1/sugerencias",
                json={},
                headers=suggestions_headers,
            )
            assert s_attempt.status_code != 429
            assert s_attempt.headers.get("ratelimit-limit") == "10"

        s_blocked = await client.post(
            "/api/v1/sugerencias",
            json={},
            headers=suggestions_headers,
        )
        assert s_blocked.status_code == 429
        assert s_blocked.headers.get("x-gateway") == "kong"
        assert s_blocked.headers.get("retry-after") is not None
        assert "API rate limit exceeded" in s_blocked.text


async def test_direct_backend_access_without_gateway_secret_is_blocked(gateway_url: str) -> None:
    """#256: FastAPI rejects direct calls lacking X-Gateway-Secret, while Kong succeeds."""
    direct_url = os.getenv("BACKEND_DIRECT_URL")
    if not direct_url:
        pytest.skip("Falta BACKEND_DIRECT_URL: prueba de bloqueo directo no ejecutada")

    async with httpx.AsyncClient(base_url=direct_url.rstrip("/"), timeout=15.0) as direct:
        no_header = await direct.get("/health")
        wrong_header = await direct.get(
            "/api/v1/catalogo/productos",
            headers={"X-Gateway-Secret": "wrong-secret"},
        )

    assert no_header.status_code == 403
    assert no_header.json()["code"] == "GATEWAY_REQUIRED"
    assert wrong_header.status_code == 403
    assert wrong_header.json()["code"] == "GATEWAY_REQUIRED"

    async with httpx.AsyncClient(base_url=gateway_url, timeout=15.0) as via_kong:
        # Even if a client sends a spoofed X-Gateway-Secret, Kong strips and replaces it.
        ok = await via_kong.get("/health", headers={"X-Gateway-Secret": "spoofed"})
    assert ok.status_code == 200
    assert ok.headers.get("x-gateway") == "kong"
