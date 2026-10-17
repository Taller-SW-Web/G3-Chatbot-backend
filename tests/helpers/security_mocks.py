"""Seguridad mock helpers (ADR-0017).

respx routes shaped like Prism over Seguridad's published `openapi.yaml`
(`:4010`, no `/api/v1` prefix, examples picked with `Prefer`).
Switching mock<->real stays config-only: tests point the client at the
mock base URL, production points it at the real module.

To run against real Prism (manual, not CI):
    prism mock <seguridad-openapi.yaml> --port 4010
then point SECURITY_BASE_URL at http://localhost:4010.
"""
import httpx
import respx


def _prism_ok(extra: dict | None = None) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, **(extra or {})})


def mock_register(base_url: str = "http://mock:4010"):
    return respx.post(f"{base_url}/auth/registro").mock(
        return_value=httpx.Response(
            201,
            json={"customerId": "cust-001", "estado": "PENDIENTE_VERIFICACION"},
        )
    )


def mock_register_conflict(base_url: str = "http://mock:4010"):
    # Prism never reveals existence: the mock answers 409 and the channel
    # maps it to a generic message (SPEC-01).
    return respx.post(f"{base_url}/auth/registro").mock(
        return_value=httpx.Response(
            409, json={"code": "CORREO_NO_DISPONIBLE", "title": "No disponible"}
        )
    )


def mock_password_policy(base_url: str = "http://mock:4010"):
    return respx.get(f"{base_url}/password/politica").mock(
        return_value=httpx.Response(
            200, json={"minLength": 8, "requireDigit": True, "requireUpper": True}
        )
    )


def mock_verify_email_ok(base_url: str = "http://mock:4010"):
    return respx.post(f"{base_url}/auth/verificar-correo").mock(
        return_value=httpx.Response(204)
    )


def mock_verify_email_expired(base_url: str = "http://mock:4010"):
    return respx.post(f"{base_url}/auth/verificar-correo").mock(
        return_value=httpx.Response(
            410, json={"code": "ENLACE_EXPIRADO", "title": "Expirado"}
        )
    )


def mock_resend_verification(base_url: str = "http://mock:4010"):
    return respx.post(f"{base_url}/auth/verificar-correo/reenviar").mock(
        return_value=httpx.Response(202, json={"ok": True})
    )


def mock_login_ok(base_url: str = "http://mock:4010"):
    return respx.post(f"{base_url}/auth/login").mock(
        return_value=httpx.Response(
            200,
            json={"accessToken": "acc", "expiresIn": 900, "usuario": {"id": "cust-001"}},
        )
    )


def mock_login_mfa(base_url: str = "http://mock:4010"):
    return respx.post(f"{base_url}/auth/login").mock(
        return_value=httpx.Response(
            200, json={"mfaRequerido": True, "challengeToken": "chg-1"}
        )
    )


def mock_login_invalid(base_url: str = "http://mock:4010"):
    # Blocked/inactive/unverified answer exactly like a wrong password.
    return respx.post(f"{base_url}/auth/login").mock(
        return_value=httpx.Response(
            401, json={"code": "CREDENCIALES_INVALIDAS", "title": "Invalid"}
        )
    )


def mock_request_mfa(base_url: str = "http://mock:4010"):
    return respx.post(f"{base_url}/auth/otp/solicitar").mock(return_value=_prism_ok())


def mock_verify_mfa(base_url: str = "http://mock:4010"):
    return respx.post(f"{base_url}/auth/otp/verificar").mock(
        return_value=httpx.Response(
            200, json={"accessToken": "acc", "expiresIn": 900}
        )
    )


def mock_refresh(base_url: str = "http://mock:4010"):
    return respx.post(f"{base_url}/auth/refresh").mock(
        return_value=httpx.Response(
            200, json={"accessToken": "acc2", "expiresIn": 900}
        )
    )


def mock_logout(base_url: str = "http://mock:4010"):
    return respx.post(f"{base_url}/auth/logout").mock(return_value=_prism_ok())


def mock_me(base_url: str = "http://mock:4010"):
    return respx.get(f"{base_url}/auth/me").mock(
        return_value=httpx.Response(
            200,
            json={"id": "cust-001", "email": "a@b.pe", "celular": "+51987654321"},
        )
    )


def mock_addresses(base_url: str = "http://mock:4010"):
    return respx.get(url__regex=r".*/usuarios/.+/direcciones").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"id": "dir-1", "distrito": "Miraflores"}]},
        )
    )


def mock_introspect_active(base_url: str = "http://mock:4010"):
    return respx.post(f"{base_url}/auth/introspeccion").mock(
        return_value=httpx.Response(200, json={"activo": True})
    )


def mock_module_down(
    method: str = "POST", path: str = "/auth/login", base_url: str = "http://mock:4010"
):
    route = respx.route(method=method, url=f"{base_url}{path}")
    route.mock(side_effect=httpx.ConnectError("mock is down"))
    return route
