"""Session use case of SPEC-03 (orchestration over the Seguridad and validator ports)."""
import logging

import pytest

from src.application.use_cases.manage_session_use_case import (
    InvalidCredentialsError,
    ManageSessionUseCase,
    RefreshInvalidError,
    mask_document,
    merge_cart_lines,
)
from src.ports.outbound.module_errors import ModuleRequestError
from src.ports.outbound.token_validator_port import AccountNotAllowedError

CUSTOMER_ID = "00000000-0000-0000-0000-000000000001"


class FakeSecurity:
    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []

    async def login(self, email, password):
        self.calls.append(("login", email))
        result = self.script.get("login")
        if isinstance(result, Exception):
            raise result
        return result

    async def request_mfa(self, challenge, canal=None):
        self.calls.append(("request_mfa", challenge, canal))
        return {"ok": True}

    async def verify_mfa(self, challenge, code):
        self.calls.append(("verify_mfa", challenge, code))
        result = self.script.get("verify_mfa")
        if isinstance(result, Exception):
            raise result
        return result

    async def refresh(self, token):
        result = self.script.get("refresh")
        if isinstance(result, Exception):
            raise result
        return result

    async def logout(self, token):
        self.calls.append(("logout", token))
        if isinstance(self.script.get("logout"), Exception):
            raise self.script["logout"]

    async def me(self, holder):
        return self.script.get("me", {"id": "cust-001"})


class FakeValidator:
    def __init__(self, error=None):
        self.error = error
        self.validated = []

    async def validate_customer(self, token):
        self.validated.append(token)
        if self.error:
            raise self.error
        return {"sub": CUSTOMER_ID}


def make_sessions(security=None, validator=None, **kwargs) -> ManageSessionUseCase:
    return ManageSessionUseCase(
        security or FakeSecurity(), validator or FakeValidator(), **kwargs
    )


@pytest.mark.asyncio
async def test_login_returns_security_answer_when_credentials_are_valid():
    """SPEC-03 Req. 1 - valid credentials open the session."""
    sessions = make_sessions(FakeSecurity({"login": {"accessToken": "acc"}}))
    answer = await sessions.login("a@b.pe", "secret123")
    assert answer["accessToken"] == "acc"


@pytest.mark.asyncio
async def test_login_with_rejected_credentials_raises_invalid_credentials():
    """SPEC-03 Req. 1 - wrong password, blocked or inactive account."""
    sessions = make_sessions(
        FakeSecurity({"login": ModuleRequestError("security", 401, "CREDENCIALES_INVALIDAS")})
    )
    with pytest.raises(InvalidCredentialsError):
        await sessions.login("a@b.pe", "wrong")


@pytest.mark.asyncio
async def test_login_with_mfa_requests_the_otp_code():
    """SPEC-03 Req. 2 - the BFF requests the code when MFA is required."""
    security = FakeSecurity({"login": {"mfaRequerido": True, "challengeToken": "chg-1",
                                       "canal": "EMAIL"}})
    answer = await make_sessions(security).login("a@b.pe", "secret123")
    assert answer["mfaRequerido"] is True
    assert ("request_mfa", "chg-1", "EMAIL") in security.calls


@pytest.mark.asyncio
async def test_login_without_customer_role_discards_session():
    """SPEC-03 Req. 1 - Scenario: Usuario sin rol CLIENTE."""
    security = FakeSecurity({"login": {"accessToken": "seller-acc", "refreshToken": "ref-1"}})
    sessions = make_sessions(security, FakeValidator(AccountNotAllowedError()))
    with pytest.raises(AccountNotAllowedError):
        await sessions.login("seller@b.pe", "secret123")
    assert ("logout", "ref-1") in security.calls


@pytest.mark.asyncio
async def test_verify_mfa_wrong_code_propagates_remaining_attempts():
    """SPEC-03 Req. 2 - Scenario: Codigo incorrecto carries `intentosRestantes`."""
    sessions = make_sessions(
        FakeSecurity({"verify_mfa": ModuleRequestError(
            "security", 401, "CODIGO_INVALIDO", intentos_restantes=2)})
    )
    with pytest.raises(InvalidCredentialsError) as exc:
        await sessions.verify_mfa_code("chg-1", "000000")
    assert exc.value.intentos_restantes == 2


@pytest.mark.asyncio
async def test_refresh_with_reused_token_raises_refresh_invalid():
    """SPEC-03 Req. 5 - reused refresh token closes every session."""
    sessions = make_sessions(
        FakeSecurity({"refresh": ModuleRequestError("security", 401, "REFRESCO_INVALIDO")})
    )
    with pytest.raises(RefreshInvalidError):
        await sessions.refresh("ref-old")


@pytest.mark.asyncio
async def test_logout_with_security_down_still_closes_local_session(caplog):
    """SPEC-03 Req. 6 - logout never fails locally and logs the failure."""
    from httpx import ConnectError
    sessions = make_sessions(FakeSecurity({"logout": ConnectError("down")}))
    with caplog.at_level(logging.WARNING):
        assert await sessions.logout("ref-1") is None
        assert await sessions.logout(None) is None
    assert any("logout" in record.message.lower() for record in caplog.records)


@pytest.mark.asyncio
async def test_profile_adds_verified_phone_and_masked_document():
    """SPEC-03 design - `/sesion/perfil` adds `celularVerificado` and `ultimoDocumento`."""

    class FakePhone:
        async def existe(self, customer_id, phone):
            return phone == "+51987654321"

    class FakeCheckouts:
        async def latest_document(self, customer_id):
            return {"tipo": "DNI", "numero": "12345678"}

    security = FakeSecurity({"me": {"id": "cust-001", "celular": "+51987654321"}})
    sessions = make_sessions(security, phone_verifications=FakePhone(), checkouts=FakeCheckouts())
    profile = await sessions.profile("holder", CUSTOMER_ID)
    assert profile["celularVerificado"] is True
    assert profile["ultimoDocumento"] == {"tipo": "DNI", "numeroEnmascarado": "****5678"}


def test_mask_document_keeps_only_the_last_four_digits():
    assert mask_document({"tipo": "DNI", "numero": "12345678"})["numeroEnmascarado"] == "****5678"


def test_merge_cart_lines_sums_same_sku_capped_at_ten():
    """SPEC-03 Req. 4 - Scenario: Ambos carritos con items."""
    merged = merge_cart_lines(
        [{"sku": "SKU-A", "cantidad": 2}],
        [{"sku": "SKU-A", "cantidad": 1}, {"sku": "SKU-B", "cantidad": 1}],
    )
    by_sku = {line["sku"]: line["cantidad"] for line in merged}
    assert by_sku == {"SKU-A": 3, "SKU-B": 1}


def test_merge_cart_lines_caps_quantity_at_ten():
    """SPEC-03 Req. 4 - quantities never exceed the per-line cap."""
    merged = merge_cart_lines([{"sku": "SKU-A", "cantidad": 9}], [{"sku": "SKU-A", "cantidad": 5}])
    assert merged[0]["cantidad"] == 10
