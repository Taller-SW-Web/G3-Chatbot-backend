"""H3-07 Seguridad mocks: typed client over BaseModuleClient + respx seeds.

Covers SPEC-01 (register, password policy), SPEC-02 (verify, resend) and
SPEC-03 (login, MFA, refresh, logout, me). No network, no Prism, no database.
"""
import httpx
import pytest
import respx

from src.adapters.outbound.http_clients.base_client import ModuleUnavailableError
from src.adapters.outbound.http_clients.security_client import SecurityClient
from src.infrastructure.config.container import provide_security_client
from src.ports.outbound.module_errors import ModuleRequestError
from tests.helpers import security_mocks

BASE_URL = "http://mock:4010"


def make_client() -> SecurityClient:
    return SecurityClient(base_url=BASE_URL, timeout_seconds=5.0)


@respx.mock
@pytest.mark.asyncio
async def test_register_sends_channel_and_returns_pending():
    route = security_mocks.mock_register(BASE_URL)
    result = await make_client().register("a@b.pe", "Passw0rd!", "+51987654321")
    assert result["estado"] == "PENDIENTE_VERIFICACION"
    import json

    assert json.loads(route.calls[0].request.content)["canalOrigen"] == "CHATBOT"


@respx.mock
@pytest.mark.asyncio
async def test_register_conflict_maps_code():
    security_mocks.mock_register_conflict(BASE_URL)
    with pytest.raises(ModuleRequestError) as exc:
        await make_client().register("a@b.pe", "Passw0rd!", "+51987654321")
    assert exc.value.code == "CORREO_NO_DISPONIBLE"
    assert exc.value.status == 409


@respx.mock
@pytest.mark.asyncio
async def test_password_policy():
    security_mocks.mock_password_policy(BASE_URL)
    result = await make_client().password_policy()
    assert result["minLength"] == 8


@respx.mock
@pytest.mark.asyncio
async def test_verify_email_ok_and_expired():
    security_mocks.mock_verify_email_ok(BASE_URL)
    await make_client().verify_email("tok")


@respx.mock
@pytest.mark.asyncio
async def test_verify_email_expired_code():
    respx.clear()
    security_mocks.mock_verify_email_expired(BASE_URL)
    with pytest.raises(ModuleRequestError) as exc:
        await make_client().verify_email("tok")
    assert exc.value.code == "ENLACE_EXPIRADO"
    assert exc.value.status == 410


@respx.mock
@pytest.mark.asyncio
async def test_resend_verification():
    security_mocks.mock_resend_verification(BASE_URL)
    result = await make_client().resend_verification("a@b.pe")
    assert result["ok"] is True


@respx.mock
@pytest.mark.asyncio
async def test_login_ok_mfa_and_invalid():
    security_mocks.mock_login_ok(BASE_URL)
    result = await make_client().login("a@b.pe", "Passw0rd!")
    assert result["accessToken"] == "acc"


@respx.mock
@pytest.mark.asyncio
async def test_login_mfa_required():
    respx.clear()
    security_mocks.mock_login_mfa(BASE_URL)
    result = await make_client().login("a@b.pe", "Passw0rd!")
    assert result["mfaRequerido"] is True


@respx.mock
@pytest.mark.asyncio
async def test_login_invalid_is_generic():
    respx.clear()
    security_mocks.mock_login_invalid(BASE_URL)
    with pytest.raises(ModuleRequestError) as exc:
        await make_client().login("a@b.pe", "wrong")
    assert exc.value.code == "CREDENCIALES_INVALIDAS"


@respx.mock
@pytest.mark.asyncio
async def test_mfa_flow():
    security_mocks.mock_request_mfa(BASE_URL)
    await make_client().request_mfa("chg-1")
    respx.clear()
    security_mocks.mock_verify_mfa(BASE_URL)
    result = await make_client().verify_mfa("chg-1", "123456")
    assert result["accessToken"] == "acc"


@respx.mock
@pytest.mark.asyncio
async def test_refresh_logout_me():
    security_mocks.mock_refresh(BASE_URL)
    assert (await make_client().refresh("rt"))["accessToken"] == "acc2"
    respx.clear()
    security_mocks.mock_logout(BASE_URL)
    await make_client().logout("rt")
    respx.clear()
    route = security_mocks.mock_me(BASE_URL)
    result = await make_client().me("holder-token")
    assert result["id"] == "cust-001"
    assert route.calls[0].request.headers["authorization"] == "Bearer holder-token"


@respx.mock
@pytest.mark.asyncio
async def test_addresses_and_introspect():
    security_mocks.mock_addresses(BASE_URL)
    rows = await make_client().list_addresses("cust-001", "holder-token")
    assert rows[0]["distrito"] == "Miraflores"
    respx.clear()
    security_mocks.mock_introspect_active(BASE_URL)
    assert (await make_client().introspect("acc"))["activo"] is True


@respx.mock
@pytest.mark.asyncio
async def test_module_down_maps_503():
    security_mocks.mock_module_down(base_url=BASE_URL)
    with pytest.raises(ModuleUnavailableError):
        await make_client().login("a@b.pe", "x")


@respx.mock
@pytest.mark.asyncio
async def test_non_json_4xx_keeps_status():
    respx.post(f"{BASE_URL}/auth/login").mock(return_value=httpx.Response(404, text="<html/>"))
    with pytest.raises(ModuleRequestError) as exc:
        await make_client().login("a@b.pe", "x")
    assert exc.value.status == 404


def test_container_wiring_uses_settings():
    client = provide_security_client()
    assert client.base_url.startswith("http")
    assert client.timeout_seconds == 5.0
    assert client.client_secret == "secreto-de-prueba"
