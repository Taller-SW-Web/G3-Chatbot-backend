"""G7 Seguridad contract: introspection (SPEC-17, ADR-002).

`POST /auth/introspeccion` authenticates the chatbot as a service and always
answers 200 with `activo`; an inactive token is not an HTTP error.
"""
import json

import httpx
import pytest
import respx

from src.shared.infrastructure.http.security_client import SecurityClient

BASE_URL = "http://mock-security:4010/api/v1"


class StubServiceTokens:
    def __init__(self) -> None:
        self.calls = 0

    async def get_token(self, *, correlation_id=None):
        self.calls += 1
        return "svc-token"


@respx.mock
@pytest.mark.asyncio
async def test_introspection_sends_the_service_token_and_reads_active(load_fixture):
    body = load_fixture("g7_introspeccion_activa.json")
    route = respx.post(f"{BASE_URL}/auth/introspeccion").mock(
        return_value=httpx.Response(200, json=body)
    )
    tokens = StubServiceTokens()
    client = SecurityClient(
        base_url=BASE_URL, timeout_seconds=5.0, service_token_provider=tokens
    )

    result = await client.introspect("user-access-token")

    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer svc-token"
    assert json.loads(request.content) == {"token": "user-access-token"}
    assert result == body
    assert result["activo"] is True
    assert result["roles"] == ["VENDEDOR"]
    assert tokens.calls == 1


@respx.mock
@pytest.mark.asyncio
async def test_introspection_inactive_is_a_200_with_motivo(load_fixture):
    body = load_fixture("g7_introspeccion_inactiva.json")
    respx.post(f"{BASE_URL}/auth/introspeccion").mock(
        return_value=httpx.Response(200, json=body)
    )
    client = SecurityClient(
        base_url=BASE_URL, timeout_seconds=5.0, service_token_provider=StubServiceTokens()
    )

    result = await client.introspect("user-access-token")

    assert result["activo"] is False
    assert result["motivo"] == "USUARIO_INACTIVO"
