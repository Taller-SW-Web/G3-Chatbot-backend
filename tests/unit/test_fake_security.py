"""In-process Seguridad fakes (ADR-0022): demo flow without network or email."""
import pytest

from src.adapters.outbound.fakes.fake_security import (
    FakeAccessTokenValidator,
    FakeSecurityAdapter,
    FakeServiceTokenProvider,
)
from src.ports.outbound.module_errors import ModuleRequestError
from src.ports.outbound.token_validator_port import InvalidTokenError


@pytest.mark.asyncio
async def test_register_verify_and_login_flow():
    security = FakeSecurityAdapter(customers=[])
    created = await security.register(
        "Nueva@Correo.com", "Segura1234", "+51987654321", first_name="Ana", last_name="Díaz"
    )
    assert created["estado"] == "PENDIENTE_VERIFICACION"
    with pytest.raises(ModuleRequestError) as blocked:
        await security.login("nueva@correo.com", "Segura1234")
    assert blocked.value.code == "CUENTA_NO_VERIFICADA"

    token = security.verification_token("nueva@correo.com")
    assert token is not None
    await security.verify_email(token)
    session = await security.login("nueva@correo.com", "Segura1234")
    assert session["accessToken"].startswith("fake-access-")
    assert session["refreshToken"].startswith("fake-refresh-")
    assert session["usuario"]["nombreCompleto"] == "Ana Díaz"


@pytest.mark.asyncio
async def test_register_with_the_same_email_is_a_conflict():
    security = FakeSecurityAdapter(customers=[])
    await security.register("a@b.pe", "Segura1234", "+51987654321")
    with pytest.raises(ModuleRequestError) as exc:
        await security.register("A@B.PE", "Otra12345", "+51987654322")
    assert exc.value.status == 409
    assert exc.value.code == "CORREO_NO_DISPONIBLE"


@pytest.mark.asyncio
async def test_verification_link_is_single_use():
    security = FakeSecurityAdapter(customers=[])
    await security.register("a@b.pe", "Segura1234", "+51987654321")
    token = security.verification_token("a@b.pe")
    await security.verify_email(token)
    with pytest.raises(ModuleRequestError) as exc:
        await security.verify_email(token)
    assert exc.value.code == "ENLACE_YA_USADO"


@pytest.mark.asyncio
async def test_expired_link_reports_expiry():
    now = [0.0]
    security = FakeSecurityAdapter(customers=[], clock=lambda: now[0])
    await security.register("a@b.pe", "Segura1234", "+51987654321")
    token = security.verification_token("a@b.pe")
    now[0] += 24 * 60 * 60 + 1
    with pytest.raises(ModuleRequestError) as exc:
        await security.verify_email(token)
    assert exc.value.code == "ENLACE_EXPIRADO"


@pytest.mark.asyncio
async def test_resend_for_an_unknown_email_stays_neutral():
    security = FakeSecurityAdapter(customers=[])
    assert await security.resend_verification("nadie@correo.com") == {}
    assert security.verification_token("nadie@correo.com") is None


@pytest.mark.asyncio
async def test_access_token_validator_accepts_minted_tokens_only():
    security = FakeSecurityAdapter()
    validator = FakeAccessTokenValidator(security)
    session = await security.login("cliente@demo.pe", "Demo1234")
    claims = await validator.validate_customer(session["accessToken"])
    assert claims["rol"] == "CLIENTE"
    with pytest.raises(InvalidTokenError):
        await validator.validate_customer("real-jwt-from-seguridad")


@pytest.mark.asyncio
async def test_fake_service_token_provider_returns_a_token():
    assert await FakeServiceTokenProvider().get_token() == "fake-service-token"
