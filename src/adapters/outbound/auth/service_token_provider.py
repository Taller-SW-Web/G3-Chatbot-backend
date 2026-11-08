"""Seguridad service token provider (A3, `/auth/token` client_credentials).

The chatbot authenticates as `modulo-chatbot` for privileged operations
(introspection before payment). Tokens are cached in memory until shortly
before `expiresIn`, so concurrent calls reuse one credential instead of
hammering Seguridad. Real credentials arrive in Hito 4; until then the mock
answers the same shape.
"""
import asyncio
import time
from typing import Any

import httpx

from src.adapters.outbound.http_clients.base_client import BaseModuleClient
from src.adapters.outbound.http_clients.security_client import parse_security_body
from src.ports.outbound.module_errors import ModuleUnavailableError

# Renew a little early so a token never expires mid-request.
CLOCK_SKEW_SECONDS = 60


class SecurityServiceTokenProvider(BaseModuleClient):
    module = "security"

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
        clock=time.monotonic,
    ):
        if not client_secret.strip():
            raise ValueError(
                "SECURITY_CLIENT_SECRET is required: set it in the environment "
                "(see .env.template); there is no code default."
            )
        super().__init__(base_url=base_url, timeout_seconds=timeout_seconds, client=client)
        self.client_id = client_id
        self.client_secret = client_secret
        self._clock = clock
        # One cache entry per (scope, audience); a token minted for one module
        # must never be served to a call for another.
        self._tokens: dict[tuple[str | None, str | None], tuple[str, float]] = {}
        self._refresh_lock = asyncio.Lock()

    def invalidate(self, rejected_token: str | None = None) -> None:
        """Drop the rejected token only; do not discard a newer one refreshed by another request."""
        if rejected_token is None:
            self._tokens.clear()
            return
        for key in [k for k, (token, _) in self._tokens.items() if token == rejected_token]:
            del self._tokens[key]

    async def get_token(
        self,
        *,
        correlation_id: str | None = None,
        scope: str | None = None,
        audience: str | None = None,
    ) -> str:
        async with self._refresh_lock:
            return await self._get_token(
                correlation_id=correlation_id,
                scope=scope or None,
                audience=audience or None,
            )

    async def _get_token(
        self,
        *,
        correlation_id: str | None = None,
        scope: str | None = None,
        audience: str | None = None,
    ) -> str:
        now = self._clock()
        key = (scope, audience)
        cached = self._tokens.get(key)
        if cached and now < cached[1] - CLOCK_SKEW_SECONDS:
            return cached[0]
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if scope:
            data["scope"] = scope
        if audience:
            data["audience"] = audience
        response = await self._request(
            "POST",
            "/auth/token",
            data=data,
            correlation_id=correlation_id,
        )
        body = self._parse_token(response)
        token = body.get("accessToken") or body.get("access_token")
        expires_in = body.get("expiresIn") or body.get("expires_in") or 900
        if not token:
            raise ModuleUnavailableError(
                self.module, "service token response has no access token"
            )
        self._tokens[key] = (str(token), now + float(expires_in))
        return str(token)

    def _parse_token(self, response: httpx.Response) -> dict[str, Any]:
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            raise ModuleUnavailableError(
                self.module, f"invalid JSON body: {exc}"
            ) from exc
        parsed = parse_security_body(self.module, response.status_code, body)
        return self.normalize_code(parsed) if isinstance(parsed, dict) else {}
