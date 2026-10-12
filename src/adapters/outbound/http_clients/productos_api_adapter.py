"""Productos catalog client (H3-06, ADR-0017).

Speaks the real Productos contract (`{PRO}/api/v1`, provisional scopes A5):
search, detail, categories, brands, prices and promotions evaluation.
Transport failures (timeout/connection/5xx) raise `ModuleUnavailableError`
and map to 503 + module upstream. External `codigo` fields normalize to
`code` on the way out.
"""
from datetime import datetime, timezone
from typing import Any

import httpx

from src.adapters.outbound.http_clients.base_client import BaseModuleClient


class ProductsClient(BaseModuleClient):
    module: str = "productos"

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 4.0,
        client: httpx.AsyncClient | None = None,
    ):
        super().__init__(base_url=base_url, timeout_seconds=timeout_seconds, client=client)

    async def search_products(
        self,
        *,
        q: str | None = None,
        category_id: str | None = None,
        brand_id: str | None = None,
        channel: str = "CHATBOT",
        page: int = 1,
        size: int = 10,
        state: str = "ACTIVO",
        min_price: float | None = None,
        max_price: float | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        if page < 1:
            raise ValueError(f"page must be >= 1, got {page}")
        if not 1 <= size <= 10:
            raise ValueError(f"size must be 1..10, got {size}")
        params: dict[str, Any] = {
            "canal": channel,
            "pagina": page,
            "tamanio": size,
            "estado": state,
        }
        if q is not None:
            params["q"] = q
        if category_id is not None:
            params["categoriaId"] = category_id
        if brand_id is not None:
            params["marcaId"] = brand_id
        if min_price is not None:
            params["precioMin"] = min_price
        if max_price is not None:
            params["precioMax"] = max_price
        response = await self._request(
            "GET", "/productos", params=params, correlation_id=correlation_id
        )
        return self.normalize_code(response.json())

    async def get_product_detail(
        self, product_id: str, *, correlation_id: str | None = None
    ) -> dict[str, Any]:
        response = await self._request(
            "GET", f"/productos/{product_id}", correlation_id=correlation_id
        )
        return self.normalize_code(response.json())

    async def list_categories(
        self, *, correlation_id: str | None = None
    ) -> list[dict[str, Any]]:
        response = await self._request(
            "GET", "/categorias", correlation_id=correlation_id
        )
        return self._unwrap_list(response.json())

    async def list_brands(
        self, *, correlation_id: str | None = None
    ) -> list[dict[str, Any]]:
        response = await self._request("GET", "/marcas", correlation_id=correlation_id)
        return self._unwrap_list(response.json())

    async def get_price(
        self, sku: str, *, correlation_id: str | None = None
    ) -> dict[str, Any]:
        response = await self._request(
            "GET", f"/precios/skus/{sku}", correlation_id=correlation_id
        )
        return self.normalize_code(response.json())

    async def get_prices(
        self,
        skus: list[str],
        *,
        channel: str = "CHATBOT",
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not skus:
            raise ValueError("skus must not be empty")
        response = await self._request(
            "GET",
            "/precios",
            params={"skus": ",".join(skus), "channel_id": channel},
            correlation_id=correlation_id,
        )
        return self._unwrap_list(response.json())

    async def evaluate_promotions(
        self,
        lines: list[dict[str, Any]],
        *,
        channel_id: str = "CHATBOT",
        coupon_code: str | None = None,
        customer_ref: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "channel_id": channel_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "lines": lines,
        }
        if coupon_code is not None:
            payload["coupon_code"] = coupon_code
        if customer_ref is not None:
            payload["customer_ref"] = customer_ref
        response = await self._request(
            "POST", "/promociones/evaluar", json=payload, correlation_id=correlation_id
        )
        return self.normalize_code(response.json())

    @staticmethod
    def _unwrap_list(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return payload["items"]
        raise ValueError(f"expected a list or paginated items, got {type(payload).__name__}")
