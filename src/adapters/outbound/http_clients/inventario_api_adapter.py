"""Productos inventory client (H3-06, SPEC-10, ADR-0017).

Live per-SKU availability, one bulk call, no cache for decisions.
Timeout is 3 s per SPEC-10 (shorter than the module default of 4 s):
stock answers must be live, never assumed.
"""
from typing import Any

import httpx

from src.adapters.outbound.http_clients.base_client import BaseModuleClient

MAX_SKUS_PER_CALL = 20
INVENTORY_TIMEOUT_SECONDS = 3.0


class InventoryClient(BaseModuleClient):
    module: str = "productos"

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = INVENTORY_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ):
        super().__init__(base_url=base_url, timeout_seconds=timeout_seconds, client=client)

    async def get_availability(
        self, skus: list[str], *, correlation_id: str | None = None
    ) -> list[dict[str, Any]]:
        if not skus:
            raise ValueError("skus must not be empty")
        if len(skus) > MAX_SKUS_PER_CALL:
            raise ValueError(f"max {MAX_SKUS_PER_CALL} SKUs per call, got {len(skus)}")
        response = await self._request(
            "GET",
            "/inventario/disponibilidad",
            params={"skus": ",".join(skus)},
            correlation_id=correlation_id,
        )
        payload = response.json()
        if isinstance(payload, list):
            return [self.normalize_code(row) for row in payload]
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return [self.normalize_code(row) for row in payload["items"]]
        raise ValueError(
            f"expected a list or paginated items, got {type(payload).__name__}"
        )

    async def get_sku_availability(
        self, sku: str, *, correlation_id: str | None = None
    ) -> dict[str, Any] | None:
        rows = await self.get_availability([sku], correlation_id=correlation_id)
        for row in rows:
            if row.get("sku") == sku:
                return row
        return None
