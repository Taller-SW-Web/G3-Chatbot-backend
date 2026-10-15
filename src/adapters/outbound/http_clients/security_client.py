"""Security and users client (H3-07, SPEC-01..03, ADR-0017).

Speaks the Seguridad contract (`{SEG}/api/v1`, confirmed): registration
with `canalOrigen: "CHATBOT"`, email verification, login with optional MFA,
refresh/logout/me, addresses and introspection.

Mock strategy: Prism serves Seguridad's published `openapi.yaml` on `:4010`
without the `/api/v1` prefix, examples picked with the `Prefer` header and
`client_secret=secreto-de-prueba` until Hito 4. Only 2xx bodies are
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

from src.adapters.outbound.http_clients.base_client import (
    BaseModuleClient,
    ModuleUnavailableError,
)
from src.ports.outbound.module_errors import ModuleRequestError

MOCK_CLIENT_SECRET = "secreto-de-prueba"

# Used when a 4xx body carries no `code`/`codigo` (or is not JSON at all).
FALLBACK_ERROR_CODES = {
    400: "VALIDACION",
    401: "CREDENCIALES_INVALIDAS",
    404: "RECURSO_NO_ENCONTRADO",
}


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
        if isinstance(body, dict):
            code = str(body.get("code", body.get("codigo", code)))
        raise ModuleRequestError(module, status_code, code)
    raise ModuleUnavailableError(module, f"unexpected HTTP {status_code}")


class SecurityBaseClient(BaseModuleClient):
    """Shared Seguridad transport: module name plus the single 2xx/4xx checkpoint."""

    module: str = "security"

    async def _parse_response(self, response: httpx.Response) -> Any:
        if response.status_code == 204:
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
        client_secret: str = MOCK_CLIENT_SECRET,
    ):
        super().__init__(base_url=base_url, timeout_seconds=timeout_seconds, client=client)
        self.client_secret = client_secret

    def _mock_headers(self, prefer: str | None = None) -> dict[str, str]:
        """Prism mock headers: example picker + mock secret (ADR-0017)."""
        headers = {"client_secret": self.client_secret}
        if prefer is not None:
            headers["Prefer"] = prefer
        return headers

    async def register(
        self, email: str, password: str, phone: str, *, correlation_id: str | None = None
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/auth/registro",
            json={"email": email, "password": password, "phone": phone, "canalOrigen": "CHATBOT"},
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
        return await self._parse_response(response)

    async def request_mfa(self, challenge_token: str, *, correlation_id: str | None = None) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/auth/otp/solicitar",
            json={"challengeToken": challenge_token},
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
        raise ValueError(f"expected a list or paginated items, got {type(body).__name__}")

    async def introspect(self, access_token: str, *, correlation_id: str | None = None) -> dict[str, Any]:
        """Pre-payment introspection (SPEC-14 RNF, A3): mocked until Hito 4."""
        response = await self._request(
            "POST",
            "/auth/introspeccion",
            json={"token": access_token},
            headers=self._mock_headers(),
            correlation_id=correlation_id,
        )
        return await self._parse_response(response)
