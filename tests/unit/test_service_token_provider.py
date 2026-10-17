"""Seguridad service token provider (A3, `/auth/token` client_credentials)."""
import httpx
import pytest
import respx

from src.adapters.outbound.auth.service_token_provider import SecurityServiceTokenProvider
from src.ports.outbound.module_errors import ModuleRequestError, ModuleUnavailableError

BASE_URL = "http://mock:4010"


def provider(clock=lambda: 0.0, secret: str = "s3cr3t"):
    return SecurityServiceTokenProvider(
        base_url=BASE_URL, client_id="modulo-chatbot", client_secret=secret, clock=clock
    )


@pytest.mark.asyncio
@respx.mock
async def test_sends_client_credentials_and_returns_the_access_token():
    route = respx.post(f"{BASE_URL}/auth/token").mock(
        return_value=httpx.Response(200, json={"accessToken": "svc-1", "expiresIn": 900})
    )
    token = await provider().get_token()
    assert token == "svc-1"
    request = route.calls[0].request
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert b"grant_type=client_credentials" in request.content
    assert b"client_id=modulo-chatbot" in request.content
    assert b"client_secret=s3cr3t" in request.content


@pytest.mark.asyncio
@respx.mock
async def test_caches_the_token_until_the_skew_before_expiry():
    now = [0.0]
    route = respx.post(f"{BASE_URL}/auth/token").mock(
        return_value=httpx.Response(200, json={"accessToken": "svc-1", "expiresIn": 900})
    )
    security = provider(clock=lambda: now[0])
    assert await security.get_token() == "svc-1"
    now[0] = 800  # still > 30 s before expiry
    assert await security.get_token() == "svc-1"
    assert route.call_count == 1
    now[0] = 880  # inside the 30 s skew: renew
    assert await security.get_token() == "svc-1"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_4xx_raises_module_request_error_with_the_contract_code():
    respx.post(f"{BASE_URL}/auth/token").mock(
        return_value=httpx.Response(401, json={"code": "CREDENCIALES_INVALIDAS"})
    )
    with pytest.raises(ModuleRequestError) as exc:
        await provider().get_token()
    assert exc.value.status == 401
    assert exc.value.code == "CREDENCIALES_INVALIDAS"


@pytest.mark.asyncio
@respx.mock
async def test_5xx_raises_module_unavailable():
    respx.post(f"{BASE_URL}/auth/token").mock(return_value=httpx.Response(500))
    with pytest.raises(ModuleUnavailableError):
        await provider().get_token()


def test_refuses_to_start_without_a_client_secret():
    with pytest.raises(ValueError, match="SECURITY_CLIENT_SECRET"):
        provider(secret="  ")
