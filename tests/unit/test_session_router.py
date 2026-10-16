"""Session HTTP edge of SPEC-03 with dependency overrides (no database, no network)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.adapters.inbound.http.problem import register_problem_handlers
from src.adapters.inbound.http.session_router import create_session_router
from src.application.use_cases.manage_session_use_case import (
    InvalidCredentialsError,
    RefreshInvalidError,
)
from src.ports.outbound.module_errors import ModuleRequestError

CSRF = {"X-Requested-With": "XMLHttpRequest"}


class FakeSessions:
    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []

    async def login(self, email, password):
        self.calls.append(("login", email))
        result = self.script.get("login")
        if isinstance(result, Exception):
            raise result
        return result

    async def request_mfa_code(self, challenge):
        self.calls.append(("request_mfa", challenge))
        result = self.script.get("request_mfa")
        if isinstance(result, Exception):
            raise result
        return result or {"ok": True}

    async def verify_mfa_code(self, challenge, code):
        self.calls.append(("verify_mfa", challenge, code))
        result = self.script.get("verify_mfa")
        if isinstance(result, Exception):
            raise result
        return result

    async def refresh(self, token):
        self.calls.append(("refresh", token))
        result = self.script.get("refresh")
        if isinstance(result, Exception):
            raise result
        return result

    async def logout(self, token):
        self.calls.append(("logout", token))

    async def profile(self, holder):
        return {"id": "cust-001"}


def make_client(sessions):
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(
        create_session_router(lambda: sessions, cookie_secure=False), prefix="/api/v1"
    )
    return TestClient(app)


BASE = "/api/v1/sesion"


def test_login_without_mfa_sets_refresh_cookie_and_returns_access_token():
    """SPEC-03 Req. 1 - Scenario: Login exitoso sin MFA."""
    sessions = FakeSessions({"login": {"accessToken": "acc", "expiresIn": 900,
                                       "refreshToken": "ref-1",
                                       "usuario": {"id": "cust-001"}}})
    res = make_client(sessions).post(BASE + "/login",
                                    json={"email": "a@b.pe", "password": "secret123"},
                                    headers=CSRF)
    assert res.status_code == 200
    assert res.json()["accessToken"] == "acc"
    assert "chat_rt=ref-1" in res.headers.get("set-cookie", "")
    assert "HttpOnly" in res.headers.get("set-cookie", "")


def test_login_with_mfa_sets_challenge_cookie_and_asks_for_code():
    """SPEC-03 Req. 2 - Scenario: Login con MFA por correo."""
    sessions = FakeSessions({"login": {"mfaRequerido": True, "challengeToken": "chg-1",
                                       "canal": "EMAIL"}})
    res = make_client(sessions).post(BASE + "/login",
                                    json={"email": "a@b.pe", "password": "secret123"},
                                    headers=CSRF)
    assert res.status_code == 200
    assert res.json() == {"mfaRequerido": True, "canal": "EMAIL"}
    assert "chat_chg=chg-1" in res.headers.get("set-cookie", "")


def test_login_with_wrong_password_returns_generic_invalid_credentials():
    """SPEC-03 Req. 1 - Scenario: Credenciales invalidas o cuenta no disponible."""
    sessions = FakeSessions({"login": InvalidCredentialsError()})
    res = make_client(sessions).post(BASE + "/login",
                                    json={"email": "a@b.pe", "password": "wrong"},
                                    headers=CSRF)
    assert res.status_code == 401
    assert res.json()["code"] == "CREDENCIALES_INVALIDAS"


def test_login_without_csrf_header_is_rejected():
    """SPEC-03 RNF - CSRF protection on /sesion/login."""
    sessions = FakeSessions({"login": {"accessToken": "acc"}})
    res = make_client(sessions).post(BASE + "/login",
                                    json={"email": "a@b.pe", "password": "secret123"})
    assert res.status_code == 403


def test_verify_mfa_code_opens_session_and_clears_challenge_cookie():
    """SPEC-03 Req. 2 - Scenario: Codigo correcto tras MFA."""
    sessions = FakeSessions({"verify_mfa": {"accessToken": "acc", "expiresIn": 900,
                                           "refreshToken": "ref-2"}})
    client = make_client(sessions)
    client.cookies.set("chat_chg", "chg-1")
    res = client.post(BASE + "/mfa/verificar", json={"code": "123456"}, headers=CSRF)
    assert res.status_code == 200
    assert res.json()["accessToken"] == "acc"


def test_verify_mfa_with_wrong_code_reports_invalid_code():
    """SPEC-03 Req. 2 - Scenario: Codigo incorrecto."""
    sessions = FakeSessions({"verify_mfa": InvalidCredentialsError()})
    client = make_client(sessions)
    client.cookies.set("chat_chg", "chg-1")
    res = client.post(BASE + "/mfa/verificar", json={"code": "000000"}, headers=CSRF)
    assert res.status_code == 401
    assert res.json()["code"] == "CODIGO_INVALIDO"


def test_verify_mfa_without_challenge_cookie_reports_expired_code():
    """SPEC-03 Req. 2 - Scenario: Intentos agotados o codigo vencido."""
    sessions = FakeSessions({"verify_mfa": {"accessToken": "acc"}})
    res = make_client(sessions).post(BASE + "/mfa/verificar", json={"code": "123456"},
                                    headers=CSRF)
    assert res.status_code == 410
    assert res.json()["code"] == "CODIGO_VENCIDO"


def test_request_mfa_beyond_rate_limit_returns_429():
    """SPEC-03 Req. 2 - Scenario: Limite de reenvios de OTP."""
    sessions = FakeSessions({"request_mfa": ModuleRequestError("security", 429, "LIMITE")})
    client = make_client(sessions)
    client.cookies.set("chat_chg", "chg-1")
    res = client.post(BASE + "/mfa/solicitar", json={"canal": "EMAIL"}, headers=CSRF)
    assert res.status_code == 429


def test_refresh_with_valid_cookie_rotates_session():
    """SPEC-03 Req. 5 - Scenario: Renovacion silenciosa."""
    sessions = FakeSessions({"refresh": {"accessToken": "acc2", "expiresIn": 900,
                                        "refreshToken": "ref-2"}})
    client = make_client(sessions)
    client.cookies.set("chat_rt", "ref-1")
    res = client.post(BASE + "/refresh", headers=CSRF)
    assert res.status_code == 200
    assert res.json()["accessToken"] == "acc2"
    assert "chat_rt=ref-2" in res.headers.get("set-cookie", "")


def test_refresh_with_reused_token_clears_cookies_and_closes_session():
    """SPEC-03 Req. 5 - Scenario: Refresh invalido o reutilizado."""
    sessions = FakeSessions({"refresh": RefreshInvalidError()})
    client = make_client(sessions)
    client.cookies.set("chat_rt", "ref-old")
    res = client.post(BASE + "/refresh", headers=CSRF)
    assert res.status_code == 401
    assert res.json()["code"] == "REFRESCO_INVALIDO"


def test_logout_clears_cookies_even_when_security_is_down():
    """SPEC-03 Req. 6 - Scenario: Logout con Seguridad caida."""
    sessions = FakeSessions()
    client = make_client(sessions)
    client.cookies.set("chat_rt", "ref-1")
    res = client.post(BASE + "/logout", headers=CSRF)
    assert res.status_code == 200
    assert sessions.calls == [("logout", "ref-1")]
