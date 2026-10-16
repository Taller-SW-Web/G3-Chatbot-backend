"""Session use case of SPEC-03 (orchestration over the Seguridad client)."""
import pytest

from src.application.use_cases.manage_session_use_case import (
    InvalidCredentialsError,
    ManageSessionUseCase,
    RefreshInvalidError,
    merge_cart_lines,
)
from src.ports.outbound.module_errors import ModuleRequestError


class FakeSecurity:
    def __init__(self, script=None):
        self.script = script or {}

    async def login(self, email, password):
        result = self.script.get("login")
        if isinstance(result, Exception):
            raise result
        return result

    async def request_mfa(self, challenge):
        return {"ok": True}

    async def verify_mfa(self, challenge, code):
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
        if isinstance(self.script.get("logout"), Exception):
            raise self.script["logout"]

    async def me(self, holder):
        return {"id": "cust-001"}


def test_login_returns_security_answer_when_credentials_are_valid():
    """SPEC-03 Req. 1 - valid credentials open the session."""
    sessions = ManageSessionUseCase(FakeSecurity({"login": {"accessToken": "acc"}}))
    import anyio
    answer = anyio.run(sessions.login, "a@b.pe", "secret123")
    assert answer["accessToken"] == "acc"


def test_login_with_rejected_credentials_raises_invalid_credentials():
    """SPEC-03 Req. 1 - wrong password, blocked or inactive account."""
    sessions = ManageSessionUseCase(
        FakeSecurity({"login": ModuleRequestError("security", 401, "CREDENCIALES_INVALIDAS")})
    )
    import anyio
    with pytest.raises(InvalidCredentialsError):
        anyio.run(sessions.login, "a@b.pe", "wrong")


def test_refresh_with_reused_token_raises_refresh_invalid():
    """SPEC-03 Req. 5 - reused refresh token closes every session."""
    sessions = ManageSessionUseCase(
        FakeSecurity({"refresh": ModuleRequestError("security", 401, "REFRESCO_INVALIDO")})
    )
    import anyio
    with pytest.raises(RefreshInvalidError):
        anyio.run(sessions.refresh, "ref-old")


def test_logout_with_security_down_still_closes_local_session():
    """SPEC-03 Req. 6 - logout never fails locally when Seguridad is down."""
    from httpx import ConnectError
    sessions = ManageSessionUseCase(FakeSecurity({"logout": ConnectError("down")}))
    import anyio
    assert anyio.run(sessions.logout, "ref-1") is None
    assert anyio.run(sessions.logout, None) is None


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
