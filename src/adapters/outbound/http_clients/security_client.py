"""Security and users client (SPEC-01..03, ADR-0017).

Speaks the Seguridad contract (`{SEG}/api/v1`, confirmed): registration
with `canalOrigen: "CHATBOT"`, email verification, login with optional MFA,
refresh/logout/me, addresses and introspection.

Mock strategy: Prism serves Seguridad's published `openapi.yaml` on `:4010`
without the `/api/v1` prefix, examples picked with the `Prefer` header and
a `client_secret` header (SECURITY_CLIENT_SECRET, mandatory: no code default) until real authentication replaces it. Only 2xx bodies are
returned as data: 4xx answers raise `ModuleRequestError` with the contract
`code`, and transport failures (timeout/connection/5xx) raise
`ModuleUnavailableError` (503 + module). External `codigo` fields
normalize to `code` on the way out.

Holder-token calls (`me`, addresses) forward the customer's `Authorization`
header (A15 pattern); the client never stores tokens.
"""
from typing import Any
from urllib.parse import quote

import httpx

from src.adapters.outbound.http_clients.base_client import BaseModuleClient
from src.ports.outbound.module_errors import (
    ModuleRequestError,
    ModuleUnavailableError,
)

# Used when a 4xx body carries no `code`/`codigo` (or is not JSON at all).
FALLBACK_ERROR_CODES = {
    400: "VALIDACION",
    401: "CREDENCIALES_INVALIDAS",
    404: "RECURSO_NO_ENCONTRADO",
}


MAX_DETAIL_CHARS = 300
MAX_FIELD_ERRORS = 20


def _clip(value: Any, limit: int = MAX_DETAIL_CHARS) -> str:
    return str(value).strip()[:limit]


def _field_errors(raw: Any) -> list[dict[str, str]]:
    """Keep only well-formed `{campo, mensaje}` rows, bounded in count and length."""
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, str]] = []
    for item in raw[:MAX_FIELD_ERRORS]:
        if not isinstance(item, dict):
            continue
        campo, mensaje = item.get("campo"), item.get("mensaje")
        if isinstance(campo, str) and isinstance(mensaje, str) and campo.strip() and mensaje.strip():
            rows.append({"campo": _clip(campo, 60), "mensaje": _clip(mensaje)})
    return rows


def parse_security_body(module: str, status_code: int, body: Any) -> Any:
    """Single checkpoint: return 2xx bodies, raise on 4xx, reject the rest.

    4xx reads `code` (or legacy `codigo`) from the body; missing codes fall
    back to `FALLBACK_ERROR_CODES` or `HTTP_{status_code}`. 5xx never reaches
    here (the base client maps it to `ModuleUnavailableError`). 1xx/3xx are
    unexpected (the client does not follow redirects), so they raise
    `ModuleUnavailableError`, never `ModuleRequestError`.
    """
    if 200 <= status_code < 300:
        return body
    if 400 <= status_code < 500:
        code = FALLBACK_ERROR_CODES.get(status_code, f"HTTP_{status_code}")
        intentos_restantes = None
        detail = ""
        errors: list[dict[str, str]] = []
        if isinstance(body, dict):
            raw_detail = body.get("detail", body.get("message"))
            if isinstance(raw_detail, str):
                detail = _clip(raw_detail)
            errors = _field_errors(body.get("errores"))
            code = str(body.get("code", body.get("codigo", code)))
            raw = body.get("intentosRestantes")
            if isinstance(raw, int):
                intentos_restantes = raw
        raise ModuleRequestError(
            module,
            status_code,
            code,
            detail,
            intentos_restantes=intentos_restantes,
            errors=errors,
        )
    raise ModuleUnavailableError(module, f"unexpected HTTP {status_code}")


class SecurityBaseClient(BaseModuleClient):
    """Shared Seguridad transport: module name plus the single 2xx/4xx checkpoint."""

    module: str = "security"

    async def _parse_response(self, response: httpx.Response) -> Any:
        if not response.content and 200 <= response.status_code < 300:
            # Any 2xx without a body (204, or 202 on resend) means "done".
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            # A non-JSON 4xx (e.g. an HTML 404 from a proxy) is still a
            # rejected request: keep the status. Anything else is a broken
            # module answer.
            if 400 <= response.status_code < 500:
                body = None
            else:
                raise ModuleUnavailableError(
                    self.module, f"invalid JSON body: {exc}"
                ) from exc
        parsed = parse_security_body(self.module, response.status_code, body)
        return self.normalize_code(parsed) if isinstance(parsed, dict) else parsed


class SecurityClient(SecurityBaseClient):
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
        client_secret: str = "",
    ):
        if not client_secret.strip():
            raise ValueError(
                "SECURITY_CLIENT_SECRET is required: set it in the environment "
                "(see .env.template); there is no code default."
            )
        super().__init__(base_url=base_url, timeout_seconds=timeout_seconds, client=client)
        self.client_secret = client_secret

    def _mock_headers(self) -> dict[str, str]:
        """Prism mock header: the client secret (ADR-0017)."""
        return {"client_secret": self.client_secret}

    async def register(
        self,
        email: str,
        password: str,
        phone: str,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "email": email,
            "password": password,
            "phone": phone,
            "canalOrigen": "CHATBOT",
        }
        # Names are optional in the Seguridad contract: only forward them when the
        # form collected them, so existing callers keep the exact same payload.
        if first_name:
            payload["nombres"] = first_name
        if last_name:
            payload["apellidos"] = last_name
        response = await self._request(
            "POST",
            "/auth/registro",
            json=payload,
            headers=self._mock_headers(),
            correlation_id=correlation_id,
        )
        return await self._parse_response(response)

    async def password_policy(self, *, correlation_id: str | None = None) -> dict[str, Any]:
        response = await self._request(
            "GET",
            "/password/politica",
            headers=self._mock_headers(),
            correlation_id=correlation_id,
        )
        return await self._parse_response(response)

    async def verify_email(self, token: str, *, correlation_id: str | None = None) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/auth/verificar-correo",
            json={"token": token},
            headers=self._mock_headers(),
            correlation_id=correlation_id,
        )
        return await self._parse_response(response)

    async def resend_verification(self, email: str, *, correlation_id: str | None = None) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/auth/verificar-correo/reenviar",
            json={"email": email},
            headers=self._mock_headers(),
            correlation_id=correlation_id,
        )
        return await self._parse_response(response)

    async def login(
        self, email: str, password: str, *, correlation_id: str | None = None
    ) -> dict[str, Any]:
        """Never distinguishes the failure cause: blocked, inactive or
        unverified accounts answer exactly like a wrong password (`401
        CREDENCIALES_INVALIDAS`), so the channel must not reveal it either."""
        response = await self._request(
            "POST",
            "/auth/login",
            json={"email": email, "password": password},
            headers=self._mock_headers(),
            correlation_id=correlation_id,
        )
        try:
            return await self._parse_response(response)
        except ModuleRequestError as exc:
            if exc.status == 401 and exc.code != "CREDENCIALES_INVALIDAS":
                raise ModuleRequestError(
                    self.module, 401, "CREDENCIALES_INVALIDAS"
                ) from None
            raise

    async def request_mfa(
        self, challenge_token: str, canal: str | None = None, *, correlation_id: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"challengeToken": challenge_token}
        if canal:
            payload["canal"] = canal
        response = await self._request(
            "POST",
            "/auth/otp/solicitar",
            json=payload,
            headers=self._mock_headers(),
            correlation_id=correlation_id,
        )
        return await self._parse_response(response)

    async def verify_mfa(
        self, challenge_token: str, code: str, *, correlation_id: str | None = None
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/auth/otp/verificar",
            json={"challengeToken": challenge_token, "code": code},
            headers=self._mock_headers(),
            correlation_id=correlation_id,
        )
        return await self._parse_response(response)

    async def refresh(self, refresh_token: str, *, correlation_id: str | None = None) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/auth/refresh",
            json={"refreshToken": refresh_token},
            headers=self._mock_headers(),
            correlation_id=correlation_id,
        )
        return await self._parse_response(response)

    async def logout(self, refresh_token: str, *, correlation_id: str | None = None) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/auth/logout",
            json={"refreshToken": refresh_token},
            headers=self._mock_headers(),
            correlation_id=correlation_id,
        )
        return await self._parse_response(response)

    async def me(self, holder_token: str, *, correlation_id: str | None = None) -> dict[str, Any]:
        """Holder-token call (A15 pattern): forwards the customer's
        `Authorization`, never the service token."""
        response = await self._request(
            "GET",
            "/auth/me",
            headers={"Authorization": f"Bearer {holder_token}", **self._mock_headers()},
            correlation_id=correlation_id,
        )
        return await self._parse_response(response)

    async def list_addresses(
        self, user_id: str, holder_token: str, *, correlation_id: str | None = None
    ) -> list[dict[str, Any]]:
        response = await self._request(
            "GET",
            f"/usuarios/{quote(user_id, safe='')}/direcciones",
            headers={"Authorization": f"Bearer {holder_token}", **self._mock_headers()},
            correlation_id=correlation_id,
        )
        body = await self._parse_response(response)
        if isinstance(body, list):
            return body
        if isinstance(body, dict) and isinstance(body.get("items"), list):
            return body["items"]
        raise ModuleUnavailableError(
            self.module,
            f"unexpected addresses shape: {type(body).__name__}",
        )

    async def introspect(self, access_token: str, *, correlation_id: str | None = None) -> dict[str, Any]:
        """Pre-payment introspection (SPEC-14 RNF, A3): mocked until the real module is available."""
        response = await self._request(
            "POST",
            "/auth/introspeccion",
            json={"token": access_token},
            headers=self._mock_headers(),
            correlation_id=correlation_id,
        )
        return await self._parse_response(response)
