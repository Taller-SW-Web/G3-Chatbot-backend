"""G7 Seguridad contract: login (SPEC-03).

Pinned contract: Modulo-de-Seguridad `specs/contrato-api/openapi.yaml` 1.2.0
(`x-estado: revision-local-no-publicada`). See tests/contract/README.md.
"""
import json

import httpx
import pytest
import respx

from src.shared.domain.module_errors import ModuleRequestError
from src.shared.infrastructure.http.security_client import SecurityClient

BASE_URL = "http://mock-security:4010/api/v1"


def make_client() -> SecurityClient:
    return SecurityClient(base_url=BASE_URL, timeout_seconds=5.0)


@respx.mock
@pytest.mark.asyncio
async def test_login_without_mfa_returns_the_documented_session(load_fixture):
    body = load_fixture("g7_login_sin_mfa.json")
    route = respx.post(f"{BASE_URL}/auth/login").mock(
        return_value=httpx.Response(200, json=body)
    )

    result = await make_client().login("maria@ejemplo.com", "Marketplace2026!")

    assert json.loads(route.calls[0].request.content) == {
        "correo": "maria@ejemplo.com",
        "contrasena": "Marketplace2026!",
    }
    assert result == body
    assert result["tokenType"] == "Bearer"
    assert result["expiresIn"] == 900
    assert result["usuario"]["roles"] == ["CLIENTE"]


@respx.mock
@pytest.mark.asyncio
async def test_login_with_mfa_returns_the_challenge_without_tokens(load_fixture):
    body = load_fixture("g7_login_con_mfa.json")
    respx.post(f"{BASE_URL}/auth/login").mock(return_value=httpx.Response(200, json=body))

    result = await make_client().login("maria@ejemplo.com", "Marketplace2026!")

    assert result["mfaRequerido"] is True
    assert result["challengeToken"] == "chg_8f2a91c4b7e5"
    assert "accessToken" not in result


@respx.mock
@pytest.mark.asyncio
async def test_login_401_uses_the_contract_code(load_fixture):
    body = load_fixture("g7_login_credenciales_invalidas.json")
    respx.post(f"{BASE_URL}/auth/login").mock(return_value=httpx.Response(401, json=body))

    with pytest.raises(ModuleRequestError) as exc:
        await make_client().login("maria@ejemplo.com", "incorrecta")

    assert exc.value.status == 401
    assert exc.value.code == "CREDENCIALES_INVALIDAS"
