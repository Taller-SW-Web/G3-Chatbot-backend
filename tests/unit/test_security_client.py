"""Seguridad mocks: typed client over BaseModuleClient + respx seeds.

Covers SPEC-01 (register, password policy), SPEC-02 (verify, resend) and
SPEC-03 (login, MFA, refresh, logout, me). No network, no Prism, no database.
"""
import json

import httpx
import pytest
import respx

from src.adapters.outbound.http_clients.base_client import ModuleUnavailableError
from src.adapters.outbound.http_clients.security_client import SecurityClient
from src.infrastructure.config.container import provide_security_client
from src.infrastructure.config.settings import get_settings
from src.ports.outbound.module_errors import ModuleRequestError
from tests.helpers import security_mocks

BASE_URL = "http://mock:4010"
TEST_SECRET = "test-secret"


def make_client() -> SecurityClient:
    return SecurityClient(base_url=BASE_URL, timeout_seconds=5.0, client_secret=TEST_SECRET)


@respx.mock
@pytest.mark.asyncio
async def test_register_sends_channel_and_returns_pending():
    route = security_mocks.mock_register(BASE_URL)
    result = await make_client().register("a@b.pe", "Passw0rd!", "+51987654321")
    assert result["estado"] == "PENDIENTE_VERIFICACION"
    payload = json.loads(route.calls[0].request.content)
    assert payload["canalOrigen"] == "CHATBOT"
    # Optional names are only sent when the form collected them.
    assert "nombres" not in payload and "apellidos" not in payload


@respx.mock
@pytest.mark.asyncio
async def test_register_forwards_optional_names():
    route = security_mocks.mock_register(BASE_URL)
    await make_client().register(
        "a@b.pe", "Passw0rd!", "+51987654321", first_name="Ana", last_name="Díaz"
    )
    payload = json.loads(route.calls[0].request.content)
    assert payload["nombres"] == "Ana"
    assert payload["apellidos"] == "Díaz"


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
async def test_verify_email_ok_returns_empty_dict():
    security_mocks.mock_verify_email_ok(BASE_URL)
    assert await make_client().verify_email("tok") == {}


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
async def test_empty_2xx_body_returns_empty_dict():
    respx.post(f"{BASE_URL}/auth/verificar-correo/reenviar").mock(
        return_value=httpx.Response(202)
    )
    assert await make_client().resend_verification("a@b.pe") == {}


@respx.mock
@pytest.mark.asyncio
async def test_non_json_2xx_body_is_module_unavailable():
    respx.post(f"{BASE_URL}/auth/verificar-correo/reenviar").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    with pytest.raises(ModuleUnavailableError):
        await make_client().resend_verification("a@b.pe")


@respx.mock
@pytest.mark.asyncio
async def test_login_ok_returns_tokens():
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
async def test_login_401_with_other_code_is_generic():
    respx.post(f"{BASE_URL}/auth/login").mock(
        return_value=httpx.Response(401, json={"code": "CUENTA_BLOQUEADA"})
    )
    with pytest.raises(ModuleRequestError) as exc:
        await make_client().login("a@b.pe", "x")
    assert exc.value.status == 401
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
async def test_list_addresses_forwards_authorization_and_escapes_user_id():
    route = security_mocks.mock_addresses(BASE_URL)
    await make_client().list_addresses("a/b c", "holder-token")
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer holder-token"
    assert request.url.raw_path == b"/usuarios/a%2Fb%20c/direcciones"


@respx.mock
@pytest.mark.asyncio
async def test_list_addresses_unexpected_shape_is_module_unavailable():
    respx.get(url__regex=r".*/usuarios/.+/direcciones").mock(
        return_value=httpx.Response(200, json={"unexpected": True})
    )
    with pytest.raises(ModuleUnavailableError):
        await make_client().list_addresses("cust-001", "holder-token")


@respx.mock
@pytest.mark.asyncio
async def test_client_secret_header_is_sent():
    route = security_mocks.mock_password_policy(BASE_URL)
    await make_client().password_policy()
    assert route.calls[0].request.headers["client_secret"] == TEST_SECRET


@respx.mock
@pytest.mark.asyncio
async def test_5xx_maps_to_module_unavailable():
    respx.post(f"{BASE_URL}/auth/login").mock(return_value=httpx.Response(502))
    with pytest.raises(ModuleUnavailableError):
        await make_client().login("a@b.pe", "x")


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


def test_container_wiring_uses_settings(monkeypatch):
    monkeypatch.setenv("SECURITY_CLIENT_SECRET", "wired-secret")
    get_settings.cache_clear()
    try:
        client = provide_security_client()
    finally:
        get_settings.cache_clear()
    assert client.base_url.startswith("http")
    assert client.timeout_seconds == 5.0
    assert client.client_secret == "wired-secret"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_client_secret_fails_loudly(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SECURITY_CLIENT_SECRET", raising=False)
    else:
        monkeypatch.setenv("SECURITY_CLIENT_SECRET", value)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="SECURITY_CLIENT_SECRET"):
            provide_security_client()
    finally:
        get_settings.cache_clear()


@respx.mock
@pytest.mark.asyncio
async def test_register_validation_keeps_bounded_field_errors():
    respx.post(f"{BASE_URL}/auth/registro").respond(
        400,
        json={
            "code": "VALIDACION",
            "errores": [
                {"campo": "phone", "mensaje": "Formato inválido"},
                {"campo": "x", "mensaje": 5},
                "junk",
            ],
        },
    )
    with pytest.raises(ModuleRequestError) as exc:
        await make_client().register("a@b.pe", "Passw0rd!", "1")
    assert exc.value.errors == [{"campo": "phone", "mensaje": "Formato inválido"}]


@respx.mock
@pytest.mark.asyncio
async def test_register_policy_keeps_bounded_detail():
    respx.post(f"{BASE_URL}/auth/registro").respond(
        422, json={"code": "POLITICA_INCUMPLIDA", "detail": "x" * 1000}
    )
    with pytest.raises(ModuleRequestError) as exc:
        await make_client().register("a@b.pe", "weak", "+51987654321")
    assert exc.value.status == 422
    assert exc.value.detail == "x" * 300
