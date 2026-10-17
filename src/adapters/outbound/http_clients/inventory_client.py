"""Productos inventory client (SPEC-10, Productos HTTP 0.5.0, ADR-0017).

Commercial availability projection: one bulk call, no cache for decisions.
Timeout is 3 s per SPEC-10: stock answers must be live, never assumed.
Only 2xx rows are returned: 4xx answers raise `ModuleRequestError` with
the contract `code`, so SPEC-10 tools can tell "not available" apart
from valid rows.

The module exposes quantities nowhere in 0.5.0: `get_availability`
returns per-SKU `status` and `verify_quantities` answers per line
(`SUFICIENTE`, `INSUFICIENTE`, `INDETERMINADO`), non-binding and without
reservation. Exact quantity enforcement happens in Ventas with 409.
"""
from typing import Any

import httpx

from src.adapters.outbound.http_clients.products_client import ProductsBaseClient
from src.ports.outbound.product_repository_port import (
    MAX_COMMERCIAL_SKUS,
    normalize_sku_list,
)

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
        self,
        skus: list[str],
        *,
        channel: str = "CHATBOT",
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clean = normalize_sku_list(skus, limit=MAX_COMMERCIAL_SKUS)
        response = await self._request(
            "GET",
            "/inventario/disponibilidad/comercial",
            params={"skus": ",".join(clean), "canal": channel},
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

    async def verify_quantities(
        self,
        lines: list[dict[str, Any]],
        *,
        channel_id: str = "CHATBOT",
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Non-binding per-line verdicts for requested quantities.

        `channel_id` follows validacion-stock/design.md; provisional until
        Productos confirms (A5/A16).
        """
        clean = self._check_lines(lines)
        response = await self._request(
            "POST",
            "/inventario/disponibilidad/comercial/verificar",
            json={"channel_id": channel_id, "lines": clean},
            correlation_id=correlation_id,
        )
        verdicts = await self._parse_response(response)
        if isinstance(verdicts, list):
            return verdicts
        if isinstance(verdicts, dict) and isinstance(verdicts.get("items"), list):
            return verdicts["items"]
        raise ValueError(
            f"expected a list or paginated items, got {type(verdicts).__name__}"
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
    def _check_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not lines:
            raise ValueError("lines must not be empty")
        clean: list[dict[str, Any]] = []
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError(f"expected a line mapping, got {line!r}")
            sku = line.get("sku")
            quantity = line.get("quantity")
            if not isinstance(sku, str) or not sku.strip() or "," in sku:
                raise ValueError(f"line sku must be a non-blank sku without commas, got {sku!r}")
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
                raise ValueError(f"line quantity must be an int >= 1, got {quantity!r}")
            clean.append({"sku": sku.strip(), "quantity": quantity})
        return clean

    @staticmethod
    def _check_row(row: Any) -> dict[str, Any]:
        """Shape-check the commercial projection; unknown statuses pass
        through and the stock validator maps them to INDETERMINADO."""
        if not isinstance(row, dict):
            raise ValueError(f"expected an availability row, got {type(row).__name__}")
        if not isinstance(row.get("sku"), str) or not row["sku"].strip():
            raise ValueError(f"row must carry a sku, got {row!r}")
        if not isinstance(row.get("status"), str) or not row["status"].strip():
            raise ValueError(f"row {row.get('sku')!r} must carry a status, got {row!r}")
        return row
