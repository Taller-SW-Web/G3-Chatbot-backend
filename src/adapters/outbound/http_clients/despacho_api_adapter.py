"""Despacho y Entrega client (`SPEC-12`, `SPEC-18`; `contratos-integracion.md` §3.4).

Calls the real module with the `modulo-chatbot` service token
(`client_credentials`, audience `api-despacho`, scopes `cotizaciones:calcular`
and `seguimientos:leer`). The token is never built here: the provider owns the
credential lifecycle (ADR-0009), so this adapter only asks for one.

Only 2xx bodies are data. A 4xx is a rejected request (`ModuleRequestError`
with the normalized `code`) and everything else is a module failure, exactly
like the other clients behind `BaseModuleClient` (ADR-0017).
"""
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from src.adapters.outbound.http_clients.base_client import BaseModuleClient
from src.ports.outbound.module_errors import (
    ModuleRequestError,
    ModuleUnavailableError,
)
from src.ports.outbound.service_token_port import ServiceTokenProvider


class DespachoClient(BaseModuleClient):
    module = "dispatch"
    audience = "api-despacho"
    quote_scope = "cotizaciones:calcular"
    tracking_scope = "seguimientos:leer"

    def __init__(
        self,
        base_url: str,
        service_token_provider: ServiceTokenProvider,
        timeout_seconds: float = 4.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url, timeout_seconds=timeout_seconds, client=client
        )
        self._tokens = service_token_provider

    async def quote_shipping(
        self,
        *,
        destino: dict[str, Any],
        lineas: list[dict[str, Any]],
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """`POST /api/v1/cotizaciones`: coverage, cost and lead time (`SPEC-12`)."""
        correlation_id = correlation_id or str(uuid.uuid4())
        response = await self._request(
            "POST",
            "/cotizaciones",
            json={"destino": destino, "lineas": lineas},
            headers=await self._authorization(correlation_id, self.quote_scope),
            correlation_id=correlation_id,
        )
        return self._parse_body(response)

    async def get_order_tracking(
        self,
        order_id: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any] | None:
        """`GET /api/v1/seguimientos/pedidos/{idPedido}` (`SPEC-18`).

        A 404 is not an error: Despacho does not know the order yet while it is
        still being prepared, so the caller gets `None`.
        """
        correlation_id = correlation_id or str(uuid.uuid4())
        response = await self._request(
            "GET",
            f"/seguimientos/pedidos/{quote(order_id, safe='')}",
            headers=await self._authorization(correlation_id, self.tracking_scope),
            correlation_id=correlation_id,
        )
        if response.status_code == 404:
            return None
        return self._parse_body(response)

    async def _authorization(
        self, correlation_id: str | None, scope: str
    ) -> dict[str, str]:
        token = await self._tokens.get_token(
            correlation_id=correlation_id,
            scope=scope,
            audience=self.audience,
        )
        return {"Authorization": f"Bearer {token}"}

    def _parse_body(self, response: httpx.Response) -> dict[str, Any]:
        if not response.content:
            body: Any = {}
        else:
            try:
                body = response.json()
            except ValueError as exc:
                # A non-JSON 4xx is still a rejected request; anything else is
                # a broken module answer.
                if not 400 <= response.status_code < 500:
                    raise ModuleUnavailableError(
                        self.module, f"invalid JSON body: {exc}"
                    ) from exc
                body = None
        if 200 <= response.status_code < 300:
            if not isinstance(body, dict):
                raise ModuleUnavailableError(
                    self.module, f"unexpected {type(body).__name__} body"
                )
            return body
        if 400 <= response.status_code < 500:
            code = f"HTTP_{response.status_code}"
            detail = ""
            if isinstance(body, dict):
                code = str(body.get("code", body.get("codigo", code)))
                raw_detail = body.get("detail", body.get("message"))
                if isinstance(raw_detail, str):
                    detail = raw_detail
            raise ModuleRequestError(self.module, response.status_code, code, detail)
        raise ModuleUnavailableError(
            self.module, f"unexpected HTTP {response.status_code}"
        )
