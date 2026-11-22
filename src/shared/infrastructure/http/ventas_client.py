"""Ventas HTTP client (SPEC-15, SPEC-17, ADR-0017).

Connects to Ventas & Postventa ({VEN}/api/v1 and {VEN}/api/v2).
Translates Ventas response codes ('codigo' -> 'code').
System actions use the service token provider (client_credentials).
Customer actions forward the customer's Bearer token.
"""
from typing import Any
from urllib.parse import quote

import httpx

from src.shared.application.ports.order_repository_port import SalesOrderPort
from src.shared.application.ports.service_token_port import ServiceTokenProvider
from src.shared.domain.module_errors import ModuleRequestError, ModuleUnavailableError
from src.shared.infrastructure.http.base_client import BaseModuleClient


class VentasClient(BaseModuleClient, SalesOrderPort):
    module: str = "sales"

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 5.0,
        service_token_provider: ServiceTokenProvider | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        super().__init__(base_url=base_url, timeout_seconds=timeout_seconds, client=client)
        self._service_tokens = service_token_provider

    async def _get_service_headers(
        self,
        correlation_id: str | None = None,
        scope: str | None = None,
        audience: str | None = "api-ventas",
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._service_tokens:
            token = await self._service_tokens.get_token(
                correlation_id=correlation_id, scope=scope, audience=audience
            )
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _parse_response(self, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json() if response.content else {}
        except ValueError as exc:
            if 400 <= response.status_code < 500:
                raise ModuleRequestError(self.module, response.status_code, f"HTTP_{response.status_code}") from exc
            raise ModuleUnavailableError(self.module, f"invalid JSON: {exc}") from exc

        if isinstance(body, dict):
            body = self.normalize_code(body)

        if 200 <= response.status_code < 300:
            return body

        if 400 <= response.status_code < 500:
            code = "ERROR_VENTAS"
            if isinstance(body, dict):
                code = str(body.get("code") or body.get("codigo") or f"HTTP_{response.status_code}")
            raise ModuleRequestError(self.module, response.status_code, code)

        raise ModuleUnavailableError(self.module, f"HTTP {response.status_code}")

    async def create_order(
        self,
        snapshot: dict[str, Any],
        *,
        idempotency_key: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/v1/pedidos (SPEC-15 Req. 1)."""
        auth_headers = await self._get_service_headers(
            correlation_id=correlation_id, scope="ventas:pedidos:crear", audience="api-ventas"
        )
        auth_headers["Idempotency-Key"] = idempotency_key

        response = await self._request(
            "POST",
            "/api/v1/pedidos",
            json=snapshot,
            headers=auth_headers,
            correlation_id=correlation_id,
        )
        return self._parse_response(response)

    async def notify_payment(
        self,
        order_id: str,
        payload: dict[str, Any],
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/v1/pedidos/{id}/pagos/notificacion (SPEC-15 Req. 2)."""
        auth_headers = await self._get_service_headers(
            correlation_id=correlation_id, scope="ventas:pagos:webhook", audience="api-ventas"
        )
        response = await self._request(
            "POST",
            f"/api/v1/pedidos/{quote(order_id, safe='')}/pagos/notificacion",
            json=payload,
            headers=auth_headers,
            correlation_id=correlation_id,
        )
        return self._parse_response(response)

    async def cancel_order(
        self,
        order_id: str,
        *,
        motivo: str = "PAGO_NO_COMPLETADO",
        comentario: str = "Tiempo límite de espera agotado; cancelado por el canal",
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/v1/pedidos/{id}/anulaciones (SPEC-15 Req. 3)."""
        auth_headers = await self._get_service_headers(
            correlation_id=correlation_id, scope="ventas:pedidos:anular", audience="api-ventas"
        )
        payload = {"motivo": motivo, "comentario": comentario}
        response = await self._request(
            "POST",
            f"/api/v1/pedidos/{quote(order_id, safe='')}/anulaciones",
            json=payload,
            headers=auth_headers,
            correlation_id=correlation_id,
        )
        return self._parse_response(response)

    async def list_customer_orders(
        self,
        holder_token: str,
        *,
        estado: str | None = None,
        desde: str | None = None,
        hasta: str | None = None,
        page: int = 1,
        page_size: int = 10,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v1/pedidos/me with customer Bearer token."""
        headers = {"Authorization": f"Bearer {holder_token.strip()}"}
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if estado:
            params["estado"] = estado
        if desde:
            params["desde"] = desde
        if hasta:
            params["hasta"] = hasta

        response = await self._request(
            "GET",
            "/api/v1/pedidos/me",
            params=params,
            headers=headers,
            correlation_id=correlation_id,
        )
        return self._parse_response(response)

    async def get_customer_order(
        self,
        order_id: str,
        holder_token: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v1/pedidos/{id} with customer Bearer token."""
        headers = {"Authorization": f"Bearer {holder_token.strip()}"}
        response = await self._request(
            "GET",
            f"/api/v1/pedidos/{quote(order_id, safe='')}",
            headers=headers,
            correlation_id=correlation_id,
        )
        return self._parse_response(response)
