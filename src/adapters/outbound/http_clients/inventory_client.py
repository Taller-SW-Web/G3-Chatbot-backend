"""Productos inventory client (H3-06, SPEC-10, ADR-0017).

Live per-SKU availability, one bulk call, no cache for decisions.
Timeout is 3 s per SPEC-10 (shorter than the module default of 4 s):
stock answers must be live, never assumed. Only 2xx rows are returned:
4xx answers raise `ModuleRequestError` with the contract `code`
(`PRODUCTO_NO_DISPONIBLE`, ...), so SPEC-10 tools can tell "not
available" apart from valid rows.
"""
from typing import Any

import httpx

from src.adapters.outbound.http_clients.products_client import ProductsBaseClient
from src.ports.outbound.product_repository_port import normalize_sku_list

MAX_SKUS_PER_CALL = 20
INVENTORY_TIMEOUT_SECONDS = 3.0


class InventoryClient(ProductsBaseClient):
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
        clean = normalize_sku_list(skus, limit=MAX_SKUS_PER_CALL)
        response = await self._request(
            "GET",
            "/inventario/disponibilidad",
            params={"skus": ",".join(clean)},
            correlation_id=correlation_id,
        )
        rows = await self._parse_response(response)
        if isinstance(rows, list):
            return [self._check_row(row) for row in rows]
        if isinstance(rows, dict) and isinstance(rows.get("items"), list):
            return [self._check_row(row) for row in rows["items"]]
        raise ValueError(
            f"expected a list or paginated items, got {type(rows).__name__}"
        )

    async def get_sku_availability(
        self, sku: str, *, correlation_id: str | None = None
    ) -> dict[str, Any] | None:
        rows = await self.get_availability([sku], correlation_id=correlation_id)
        for row in rows:
            if row.get("sku") == sku.strip():
                return row
        return None

    @staticmethod
    def _check_row(row: Any) -> dict[str, Any]:
        """Reject nonsense stock figures instead of passing them downstream."""
        if not isinstance(row, dict):
            raise ValueError(f"expected an availability row, got {type(row).__name__}")
        for field in ("on_hand", "reserved", "available"):
            value = row.get(field)
            if value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(
                    f"row {row.get('sku')!r}: {field} must be a number, got {value!r}"
                )
            if value < 0:
                raise ValueError(
                    f"row {row.get('sku')!r}: {field} must be >= 0, got {value!r}"
                )
        return row
