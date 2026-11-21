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
Rows outside the published enum, missing, extra or duplicated are absence
("not obtained"), never a commercial state (A17 · D2).
"""
from typing import Any

import httpx

from src.adapters.outbound.http_clients.products_client import (
    SCOPE_INVENTORY,
    ProductsBaseClient,
)
from src.ports.outbound.module_errors import ModuleUnavailableError
from src.ports.outbound.product_repository_port import (
    MAX_COMMERCIAL_SKUS,
    normalize_sku_list,
)

INVENTORY_TIMEOUT_SECONDS = 3.0

# The only commercial states the contract publishes (D2): anything else in the
# payload is an incompatible answer, not a commercial state.
KNOWN_STATUSES = frozenset({"DISPONIBLE", "STOCK_BAJO", "AGOTADO", "INDETERMINADO"})
KNOWN_RESULTS = frozenset({"SUFICIENTE", "INSUFICIENTE", "INDETERMINADO"})


class InventoryClient(ProductsBaseClient):
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = INVENTORY_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
        service_token_provider=None,
    ):
        super().__init__(base_url=base_url, timeout_seconds=timeout_seconds, client=client)
        self._service_token_provider = service_token_provider

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
            scope=SCOPE_INVENTORY,
        )
        rows = await self._parse_response(response)
        if isinstance(rows, dict):
            rows = rows.get("items")
        if not isinstance(rows, list):
            raise ModuleUnavailableError(
                self.module, f"expected availability items, got {type(rows).__name__}"
            )
        return self._valid_rows(rows, clean)

    @classmethod
    def _valid_rows(cls, rows, requested: list[str]):
        """Exact SKU correspondence: missing, unknown or extra rows are absence.

        A row only counts when its SKU was requested, its `status` belongs to the
        published enum and its shape matches the projection. Everything else is
        "availability not obtained", never `AGOTADO` nor `INDETERMINADO`.
        """
        wanted = set(requested)
        valid = []
        seen: set[str] = set()
        for row in rows:
            try:
                clean = cls._check_row(row)
            except ValueError:
                continue
            sku = str(clean["sku"]).strip()
            if sku in wanted and sku not in seen:
                valid.append({**clean, "sku": sku})
                seen.add(sku)
        return valid

    async def verify_quantities(
        self,
        lines: list[dict[str, Any]],
        *,
        channel_id: str = "CHATBOT",
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Non-binding per-line verdicts for requested quantities.

        G6 OpenAPI a7778ec requires the delivery fulfillment context and answers
        `items[{sku, requested_quantity, result, reason_code}]`; callers read
        `result` directly (no internal rename). Rows that are missing, extra,
        duplicated or out of the published enum are discarded so the caller sees
        "not obtained" instead of a commercial state.
        """
        clean = self._check_lines(lines)
        response = await self._request(
            "POST",
            "/inventario/disponibilidad/comercial/verificar",
            json={"channel_id": channel_id, "fulfillment": {"mode": "ENVIO_DOMICILIO"}, "lines": clean},
            correlation_id=correlation_id,
            scope=SCOPE_INVENTORY,
        )
        verdicts = await self._parse_response(response)
        if isinstance(verdicts, dict):
            verdicts = verdicts.get("items")
        if not isinstance(verdicts, list):
            raise ModuleUnavailableError(
                self.module, f"expected verdict items, got {type(verdicts).__name__}"
            )
        return self._valid_verdicts(verdicts, clean)

    @staticmethod
    def _valid_verdicts(rows, lines: list[dict[str, Any]]):
        requested = {line["sku"] for line in lines}
        valid = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            sku = row.get("sku")
            result = row.get("result")
            quantity = row.get("requested_quantity")
            if (not isinstance(sku, str) or sku.strip() not in requested
                    or sku.strip() in seen
                    or not isinstance(result, str) or result not in KNOWN_RESULTS
                    or not isinstance(quantity, int) or isinstance(quantity, bool)):
                continue
            valid.append({
                "sku": sku.strip(),
                "requested_quantity": quantity,
                "result": result,
                "reason_code": row.get("reason_code"),
            })
            seen.add(sku.strip())
        return valid

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
        """Shape-check the commercial projection: `sku` plus a published `status`."""
        if not isinstance(row, dict):
            raise ValueError(f"expected an availability row, got {type(row).__name__}")
        if not isinstance(row.get("sku"), str) or not row["sku"].strip():
            raise ValueError(f"row must carry a sku, got {row!r}")
        status = row.get("status")
        if not isinstance(status, str) or status not in KNOWN_STATUSES:
            raise ValueError(f"row {row.get('sku')!r} has an unpublished status, got {row!r}")
        return {"sku": row["sku"], "status": status}
