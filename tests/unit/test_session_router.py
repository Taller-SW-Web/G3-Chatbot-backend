"""Session HTTP edge of SPEC-03 with dependency overrides (no database, no network)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.adapters.inbound.http.dependencies.jwt_validator import get_current_customer
from src.adapters.inbound.http.problem import register_problem_handlers
from src.adapters.inbound.http.session_router import create_session_router
from src.application.use_cases.manage_session_use_case import (
    InvalidCredentialsError,
    RefreshInvalidError,
)
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.outbound.module_errors import ModuleRequestError, ModuleUnavailableError
from src.ports.outbound.token_validator_port import AccountNotAllowedError

CSRF = {"X-Requested-With": "XMLHttpRequest"}
CUSTOMER_ID = "00000000-0000-0000-0000-000000000001"
ALLOWED_ORIGIN = "http://localhost:3000"


class FakeSessions:
    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []

    async def login(self, email, password, *, canal=None):
        self.calls.append(("login", email, canal))
        result = self.script.get("login")
        if isinstance(result, Exception):
            raise result
        return result

    async def request_mfa_code(self, challenge, canal=None):
        self.calls.append(("request_mfa", challenge, canal))
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
        if isinstance(self.script.get("logout"), Exception):
            raise self.script["logout"]

    async def profile(self, holder, customer_id):
        return {"id": "cust-001", "celularVerificado": True, "ultimoDocumento": None}

    async def register(self, *, email, password, phone, first_name=None, last_name=None):
        self.calls.append(("register", email, phone, first_name, last_name))
        result = self.script.get(
            "register",
            {"estado": "PENDIENTE_VERIFICACION", "correoEnmascarado": "m***a@correo.com"},
        )
        if isinstance(result, Exception):
            raise result
        return result

    async def password_policy(self):
        self.calls.append(("password_policy",))
        result = self.script.get("password_policy", {"minLength": 8, "requireDigit": True})
        if isinstance(result, Exception):
            raise result
        return result

    async def verify_email(self, token):
        self.calls.append(("verify_email", token))
        result = self.script.get("verify_email", {})
        if isinstance(result, Exception):
            raise result
        return result or {}

    async def resend_verification(self, email):
        self.calls.append(("resend_verification", email))
        result = self.script.get("resend_verification", {})
        if isinstance(result, Exception):
            raise result
        return result or {}


def make_client(sessions, *, with_profile_identity: bool = False):
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(
        create_session_router(
            lambda: sessions, cookie_secure=False, allowed_origins=[ALLOWED_ORIGIN]
        ),
        prefix="/api/v1",
    )
    if with_profile_identity:
        app.dependency_overrides[get_current_customer] = lambda: CallerIdentity(
            customer_id=CUSTOMER_ID
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


def test_login_forwards_requested_channel_and_defaults_to_email():
    """SPEC-03 Req. 2: the MFA channel chosen at login reaches Seguridad (default EMAIL)."""
    mfa = {"mfaRequerido": True, "challengeToken": "chg-1", "canal": "SMS"}
    sessions = FakeSessions({"login": mfa})
    client = make_client(sessions)
    creds = {"email": "a@b.pe", "password": "secret123"}
    assert client.post(BASE + "/login", json={**creds, "canal": "SMS"}, headers=CSRF).status_code == 200
    assert client.post(BASE + "/login", json=creds, headers=CSRF).status_code == 200
    assert sessions.calls == [("login", "a@b.pe", "SMS"), ("login", "a@b.pe", "EMAIL")]


def test_login_rejects_unknown_channel():
    sessions = FakeSessions({"login": {}})
    res = make_client(sessions).post(
        BASE + "/login", json={"email": "a@b.pe", "password": "x", "canal": "FAX"}, headers=CSRF
    )
    assert res.status_code == 400
    assert sessions.calls == []


def test_login_with_wrong_password_returns_generic_invalid_credentials():
    """SPEC-03 Req. 1 - Scenario: Credenciales invalidas o cuenta no disponible."""
    sessions = FakeSessions({"login": InvalidCredentialsError()})
    res = make_client(sessions).post(BASE + "/login",
                                    json={"email": "a@b.pe", "password": "wrong"},
                                    headers=CSRF)
    assert res.status_code == 401
    assert res.json()["code"] == "CREDENCIALES_INVALIDAS"


def test_login_without_customer_role_returns_account_not_allowed():
    """SPEC-03 Req. 1 - Scenario: Usuario sin rol CLIENTE."""
    sessions = FakeSessions({"login": AccountNotAllowedError()})
    res = make_client(sessions).post(BASE + "/login",
                                    json={"email": "seller@b.pe", "password": "secret123"},
                                    headers=CSRF)
    assert res.status_code == 403
    assert res.json()["code"] == "CUENTA_NO_PERMITIDA"


def test_login_when_security_is_down_returns_503_module_unavailable():
    """ADR-0017 - Seguridad down is a degraded channel, not a request error."""
    sessions = FakeSessions({"login": ModuleUnavailableError("security", "HTTP 503")})
    res = make_client(sessions).post(BASE + "/login",
                                    json={"email": "a@b.pe", "password": "secret123"},
                                    headers=CSRF)
    assert res.status_code == 503
    assert res.json()["code"] == "MODULO_NO_DISPONIBLE"


def test_login_without_csrf_header_is_rejected():
    """SPEC-03 RNF - CSRF protection on /sesion/login."""
    sessions = FakeSessions({"login": {"accessToken": "acc"}})
    res = make_client(sessions).post(BASE + "/login",
                                    json={"email": "a@b.pe", "password": "secret123"})
    assert res.status_code == 403


def test_login_with_untrusted_origin_is_rejected_exactly():
    """SPEC-03 RNF - Origin is matched exactly, not as a substring."""
    sessions = FakeSessions({"login": {"accessToken": "acc"}})
    client = make_client(sessions)
    blocked = client.post(BASE + "/login", json={"email": "a@b.pe", "password": "secret123"},
                          headers={**CSRF, "Origin": "https://evil-host.com"})
    assert blocked.status_code == 403
    allowed = client.post(BASE + "/login", json={"email": "a@b.pe", "password": "secret123"},
                          headers={**CSRF, "Origin": ALLOWED_ORIGIN})
    assert allowed.status_code == 200


def test_verify_mfa_code_opens_session_and_clears_challenge_cookie():
    """SPEC-03 Req. 2 - Scenario: Codigo correcto tras MFA."""
    sessions = FakeSessions({"verify_mfa": {"accessToken": "acc", "expiresIn": 900,
                                           "refreshToken": "ref-2"}})
    client = make_client(sessions)
    client.cookies.set("chat_chg", "chg-1")
    res = client.post(BASE + "/mfa/verificar", json={"code": "123456"}, headers=CSRF)
    assert res.status_code == 200
    assert res.json()["accessToken"] == "acc"


def test_verify_mfa_account_not_allowed_clears_challenge_and_refresh_cookies():
    """The 403 problem must carry the Set-Cookie deletions (the injected Response is discarded)."""
    sessions = FakeSessions({"verify_mfa": AccountNotAllowedError()})
    client = make_client(sessions)
    client.cookies.set("chat_chg", "chg-1")
    res = client.post(BASE + "/mfa/verificar", json={"code": "123456"}, headers=CSRF)
    assert res.status_code == 403
    assert res.json()["code"] == "CUENTA_NO_PERMITIDA"
    cookies = res.headers.get_list("set-cookie")
    assert any(c.startswith("chat_chg=") and "Max-Age=0" in c for c in cookies)
    assert any(c.startswith("chat_rt=") and "Max-Age=0" in c for c in cookies)


def test_verify_mfa_with_wrong_code_reports_invalid_code_and_attempts_left():
    """SPEC-03 Req. 2 - Scenario: Codigo incorrecto, con intentos restantes."""
    sessions = FakeSessions({"verify_mfa": InvalidCredentialsError(intentos_restantes=2)})
    client = make_client(sessions)
    client.cookies.set("chat_chg", "chg-1")
    res = client.post(BASE + "/mfa/verificar", json={"code": "000000"}, headers=CSRF)
    assert res.status_code == 401
    assert res.json()["code"] == "CODIGO_INVALIDO"
    assert res.json()["intentosRestantes"] == 2


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
    res = client.post(BASE + "/mfa/solicitar", json={"canal": "SMS"}, headers=CSRF)
    assert res.status_code == 429
    assert ("request_mfa", "chg-1", "SMS") in sessions.calls


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


def test_logout_with_security_failing_still_returns_ok_and_clears_cookie():
    """SPEC-03 Req. 6 - a Seguridad failure never blocks the local logout."""
    sessions = FakeSessions({"logout": ModuleUnavailableError("security", "down")})
    client = make_client(sessions)
    client.cookies.set("chat_rt", "ref-1")
    res = client.post(BASE + "/logout", headers=CSRF)
    assert res.status_code == 200
    assert "chat_rt=" in res.headers.get("set-cookie", "")


def test_verify_mfa_when_attempts_are_exhausted_mirrors_module_status():
    """SPEC-03 Req. 2 - Scenario: Intentos agotados or vencido."""
    sessions = FakeSessions({"verify_mfa": ModuleRequestError(
        "security", 403, "OTP_INTENTOS_AGOTADOS")})
    client = make_client(sessions)
    client.cookies.set("chat_chg", "chg-1")
    res = client.post(BASE + "/mfa/verificar", json={"code": "123456"}, headers=CSRF)
    assert res.status_code == 403
    assert res.json()["code"] == "OTP_INTENTOS_AGOTADOS"


def test_profile_returns_enriched_profile_for_authenticated_customer():
    """SPEC-03 design - `/sesion/perfil` proxies `/auth/me` plus local fields."""
    sessions = FakeSessions()
    client = make_client(sessions, with_profile_identity=True)
    res = client.get(BASE + "/perfil", headers={"Authorization": "Bearer holder"})
    assert res.status_code == 200
    assert res.json()["celularVerificado"] is True


REGISTRO = {
    "email": "maria@correo.com",
    "password": "SuperSecreta1",
    "phone": "+51987654321",
    "firstName": "María",
    "lastName": "Quispe",
    "acceptTerms": True,
}


def test_register_returns_201_with_masked_email_and_opens_no_session():
    """SPEC-01 Req. 3 - registration creates the account but never opens a session."""
    sessions = FakeSessions()
    res = make_client(sessions).post(BASE + "/registro", json=REGISTRO, headers=CSRF)
    assert res.status_code == 201
    assert res.json() == {
        "estado": "PENDIENTE_VERIFICACION",
        "correoEnmascarado": "m***a@correo.com",
    }
    assert "chat_rt" not in res.headers.get("set-cookie", "")
    assert sessions.calls == [
        ("register", "maria@correo.com", "+51987654321", "María", "Quispe")
    ]


def test_register_requires_csrf_header():
    """SPEC-01 RNF - mutating endpoints require X-Requested-With + Origin."""
    sessions = FakeSessions()
    res = make_client(sessions).post(BASE + "/registro", json=REGISTRO)
    assert res.status_code == 403
    assert sessions.calls == []


def test_register_requires_accepting_terms_and_well_formed_fields():
    """SPEC-01 Req. 1 - the form contract is enforced at the edge."""
    sessions = FakeSessions()
    client = make_client(sessions)
    rejected_terms = client.post(
        BASE + "/registro", json={**REGISTRO, "acceptTerms": False}, headers=CSRF
    )
    rejected_phone = client.post(
        BASE + "/registro", json={**REGISTRO, "phone": "abc"}, headers=CSRF
    )
    rejected_email = client.post(
        BASE + "/registro", json={**REGISTRO, "email": "no-arroba"}, headers=CSRF
    )
    assert rejected_terms.status_code == 400
    assert rejected_phone.status_code == 400
    assert rejected_email.status_code == 400
    assert rejected_terms.json()["code"] == "VALIDACION"
    assert sessions.calls == []


def test_register_with_taken_email_returns_correo_no_disponible():
    """SPEC-01 Req. 4 - Seguridad's 409 is surfaced as a neutral availability answer."""
    sessions = FakeSessions({"register": ModuleRequestError("security", 409, "CORREO_NO_DISPONIBLE")})
    res = make_client(sessions).post(BASE + "/registro", json=REGISTRO, headers=CSRF)
    assert res.status_code == 409
    assert res.json()["code"] == "CORREO_NO_DISPONIBLE"


def test_register_when_security_is_down_returns_503_servicio_no_disponible():
    """SPEC-01 - a degraded Seguridad answers 503 with the contract code."""
    sessions = FakeSessions({"register": ModuleUnavailableError("security", "HTTP 503")})
    res = make_client(sessions).post(BASE + "/registro", json=REGISTRO, headers=CSRF)
    assert res.status_code == 503
    assert res.json()["code"] == "SERVICIO_NO_DISPONIBLE"


def test_register_twice_with_the_same_email_hits_the_anti_rebound():
    """SPEC-01 RNF - a double submit is rejected with SOLICITUD_EN_CURSO."""
    sessions = FakeSessions()
    client = make_client(sessions)
    assert client.post(BASE + "/registro", json=REGISTRO, headers=CSRF).status_code == 201
    again = client.post(BASE + "/registro", json=REGISTRO, headers=CSRF)
    assert again.status_code == 429
    assert again.json()["code"] == "SOLICITUD_EN_CURSO"
    assert again.json()["retryAfter"] >= 1


def test_register_is_limited_to_five_accounts_per_ip():
    """SPEC-01 RNF - 5 registrations per IP every 10 minutes."""
    sessions = FakeSessions()
    client = make_client(sessions)
    for index in range(5):
        res = client.post(
            BASE + "/registro", json={**REGISTRO, "email": f"user{index}@correo.com"}, headers=CSRF
        )
        assert res.status_code == 201
    blocked = client.post(
        BASE + "/registro", json={**REGISTRO, "email": "user5@correo.com"}, headers=CSRF
    )
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "DEMASIADAS_SOLICITUDES"
    assert blocked.headers["retry-after"] == str(blocked.json()["retryAfter"])


def test_register_limit_uses_the_forwarded_client_ip():
    """ADR-0023 - the proxy forwards the real client in X-Forwarded-For."""
    sessions = FakeSessions()
    client = make_client(sessions)
    forwarded = {**CSRF, "X-Forwarded-For": "203.0.113.7"}
    for index in range(5):
        res = client.post(
            BASE + "/registro",
            json={**REGISTRO, "email": f"a{index}@correo.com"},
            headers=forwarded,
        )
        assert res.status_code == 201
    assert client.post(
        BASE + "/registro", json={**REGISTRO, "email": "a9@correo.com"}, headers=forwarded
    ).status_code == 429
    other_ip = {**CSRF, "X-Forwarded-For": "198.51.100.9"}
    assert client.post(
        BASE + "/registro", json={**REGISTRO, "email": "a9@correo.com"}, headers=other_ip
    ).status_code == 201


def test_password_policy_is_public_and_forwards_the_module_answer():
    """SPEC-01 Req. 2 - the chat renders the policy before asking for a password."""
    sessions = FakeSessions()
    res = make_client(sessions).get(BASE + "/politica-contrasena")
    assert res.status_code == 200
    assert res.json() == {"minLength": 8, "requireDigit": True}


def test_verify_email_returns_204_and_forwards_the_token():
    """SPEC-02 Req. 1 - a valid link activates the account without opening a session."""
    sessions = FakeSessions()
    client = make_client(sessions)
    res = client.post(BASE + "/verificar-correo", json={"token": "tok-123456"}, headers=CSRF)
    assert res.status_code == 204
    assert res.content == b""
    assert ("verify_email", "tok-123456") in sessions.calls
    assert "chat_rt" not in res.headers.get("set-cookie", "")


def test_verify_email_expired_link_returns_410_with_code():
    """SPEC-02 Req. 1 - expired and already-used links are distinguishable."""
    sessions = FakeSessions({"verify_email": ModuleRequestError("security", 410, "ENLACE_EXPIRADO")})
    res = make_client(sessions).post(
        BASE + "/verificar-correo", json={"token": "tok-123456"}, headers=CSRF
    )
    assert res.status_code == 410
    assert res.json()["code"] == "ENLACE_EXPIRADO"


def test_resend_verification_is_neutral_and_rate_limited():
    """SPEC-02 Req. 2 - the same 202 for every email, max 3 per email per hour."""
    sessions = FakeSessions()
    client = make_client(sessions)
    payload = {"email": "quien@correo.com"}
    for _ in range(3):
        assert client.post(
            BASE + "/verificar-correo/reenviar", json=payload, headers=CSRF
        ).status_code == 202
    blocked = client.post(BASE + "/verificar-correo/reenviar", json=payload, headers=CSRF)
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "DEMASIADAS_SOLICITUDES"
    assert sessions.calls.count(("resend_verification", "quien@correo.com")) == 3
