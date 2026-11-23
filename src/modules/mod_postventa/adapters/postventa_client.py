"""G5 M2 Postventa client (F3, F5, F6).

Uses the customer bearer token for every operation (2026-11-17 agreement):
G5 validates ownership and links claims/returns to the authenticated `sub`.
The base URL is the M2 Render service, separate from M1.
"""
from typing import Any
from urllib.parse import quote

import httpx

from src.modules.mod_postventa.ports.postventa_port import PostventaPort
from src.shared.domain.module_errors import ModuleRequestError, ModuleUnavailableError
from src.shared.infrastructure.http.base_client import BaseModuleClient


class PostventaClient(BaseModuleClient, PostventaPort):
    module: str = "postventa"

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ):
        super().__init__(base_url=base_url, timeout_seconds=timeout_seconds, client=client)

    @staticmethod
    def _customer_headers(holder_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {holder_token.strip()}"}

    def _parse_response(self, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json() if response.content else {}
        except ValueError as exc:
            if 400 <= response.status_code < 500:
                raise ModuleRequestError(
                    self.module, response.status_code, f"HTTP_{response.status_code}"
                ) from exc
            raise ModuleUnavailableError(self.module, f"invalid JSON: {exc}") from exc

        if isinstance(body, dict):
            body = self.normalize_code(body)

        if 200 <= response.status_code < 300:
            return body

        if 400 <= response.status_code < 500:
            code = "ERROR_POSTVENTA"
            if isinstance(body, dict):
                code = str(body.get("code") or body.get("codigo") or f"HTTP_{response.status_code}")
            raise ModuleRequestError(self.module, response.status_code, code)

        raise ModuleUnavailableError(self.module, f"HTTP {response.status_code}")

    async def upload_evidence(
        self,
        *,
        file_name: str,
        content_type: str,
        content: bytes,
        holder_token: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/api/v2/devoluciones/evidencias/upload",
            files={"file": (file_name, content, content_type or "application/octet-stream")},
            headers=self._customer_headers(holder_token),
            correlation_id=correlation_id,
        )
        return self._parse_response(response)

    async def create_return(
        self,
        payload: dict[str, Any],
        *,
        holder_token: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/api/v2/devoluciones",
            json=payload,
            headers=self._customer_headers(holder_token),
            correlation_id=correlation_id,
        )
        return self._parse_response(response)

    async def list_customer_returns(
        self,
        holder_token: str,
        *,
        estado: str | None = None,
        tipo: str | None = None,
        page: int = 1,
        page_size: int = 10,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"pagina": max(page - 1, 0), "tamano": page_size}
        if estado:
            params["estado"] = estado
        if tipo:
            params["tipo"] = tipo
        response = await self._request(
            "GET",
            "/api/v2/devoluciones/me",
            params=params,
            headers=self._customer_headers(holder_token),
            correlation_id=correlation_id,
        )
        return self._parse_response(response)

    async def get_customer_return(
        self,
        return_id: str,
        holder_token: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"/api/v2/devoluciones/{quote(return_id, safe='')}",
            headers=self._customer_headers(holder_token),
            correlation_id=correlation_id,
        )
        return self._parse_response(response)

    async def create_claim(
        self,
        payload: dict[str, Any],
        *,
        holder_token: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/api/v2/reclamos",
            json=payload,
            headers=self._customer_headers(holder_token),
            correlation_id=correlation_id,
        )
        return self._parse_response(response)

    async def list_customer_claims(
        self,
        holder_token: str,
        *,
        estado: str | None = None,
        page: int = 1,
        page_size: int = 10,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"pagina": max(page - 1, 0), "tamano": page_size}
        if estado:
            params["estado"] = estado
        response = await self._request(
            "GET",
            "/api/v2/reclamos/me",
            params=params,
            headers=self._customer_headers(holder_token),
            correlation_id=correlation_id,
        )
        return self._parse_response(response)

    async def get_claim(
        self,
        code: str,
        holder_token: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"/api/v2/reclamos/{quote(code, safe='')}",
            headers=self._customer_headers(holder_token),
            correlation_id=correlation_id,
        )
        return self._parse_response(response)

    async def create_csat(
        self,
        payload: dict[str, Any],
        *,
        holder_token: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/api/v2/csat",
            json=payload,
            headers=self._customer_headers(holder_token),
            correlation_id=correlation_id,
        )
        return self._parse_response(response)
