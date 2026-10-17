"""Stock validation over the commercial projection (SPEC-10, Productos HTTP 0.5.0).

The module exposes no quantities: the projection answers per-SKU `status`
and `verify_quantities` answers per line. Exact enforcement happens in
Ventas with 409. Unknown statuses map to INDETERMINADO, never to available.
"""
from dataclasses import dataclass

IN_STOCK = "DISPONIBLE"
LOW_STOCK = "STOCK_BAJO"
OUT_OF_STOCK = "AGOTADO"
UNKNOWN_STOCK = "INDETERMINADO"

WARNING_STATUSES = {LOW_STOCK, UNKNOWN_STOCK}

ENOUGH = "SUFICIENTE"
SHORT = "INSUFICIENTE"
UNDECIDED = "INDETERMINADO"


@dataclass(frozen=True)
class StockCheck:
    ok: bool
    code: str
    warning: str | None = None


class StockValidator:
    def check_row(self, row: dict | None) -> StockCheck:
        """Single projection row: available, low/unknown with warning, or out."""
        status = row.get("status") if isinstance(row, dict) else None
        if status == IN_STOCK:
            return StockCheck(True, "OK")
        if status in WARNING_STATUSES:
            return StockCheck(True, str(status), warning=str(status))
        if status == OUT_OF_STOCK:
            return StockCheck(False, "SKU_AGOTADO")
        return StockCheck(False, UNKNOWN_STOCK, warning=UNKNOWN_STOCK)

    def check_bulk(self, rows: list[dict]) -> dict[str, StockCheck]:
        return {
            str(row.get("sku")): self.check_row(row)
            for row in rows
            if isinstance(row, dict) and row.get("sku")
        }

    def check_quantity(self, verdict: str | None) -> StockCheck:
        """Quantity verdict: enough, short, or undecided (warning)."""
        if verdict == ENOUGH:
            return StockCheck(True, "OK")
        if verdict == SHORT:
            return StockCheck(False, "STOCK_INSUFICIENTE")
        return StockCheck(True, UNDECIDED, warning=UNDECIDED)
