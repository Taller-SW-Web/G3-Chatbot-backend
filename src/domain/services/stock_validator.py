"""Stock validation over the commercial projection (SPEC-10, Productos HTTP 0.5.0).

The module exposes no quantities: the projection answers per-SKU `status`
and `verify_quantities` answers per line. `INDETERMINADO` is a valid contract
value (accepted with a warning); a missing row, a malformed payload or an
unknown value is *availability not obtained* and uses its own code
(`SERVICIO_NO_DISPONIBLE`), never a commercial state (A17 · D2). Exact
enforcement happens in Ventas with 409.
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

# Distinct from the contract's `INDETERMINADO`: Productos did not answer.
NOT_OBTAINED = "SERVICIO_NO_DISPONIBLE"

# Customer-facing warnings per state; `INDETERMINADO` is never "few units".
STATUS_WARNINGS = {
    LOW_STOCK: "Quedan pocas unidades; el stock no se reserva hasta completar el pago.",
    UNKNOWN_STOCK: "No pudimos confirmar el stock en este momento; lo verificaremos antes del pago.",
}


@dataclass(frozen=True)
class StockCheck:
    ok: bool
    code: str
    warning: str | None = None

    @property
    def message(self) -> str | None:
        return STATUS_WARNINGS.get(self.warning) if self.warning else None


class StockValidator:
    def check_row(self, row: dict | None) -> StockCheck:
        """Single projection row: available, low/unknown with warning, out, or absent."""
        status = row.get("status") if isinstance(row, dict) else None
        if status == IN_STOCK:
            return StockCheck(True, "OK")
        if status == LOW_STOCK:
            return StockCheck(True, LOW_STOCK, warning=LOW_STOCK)
        if status == UNKNOWN_STOCK:
            # A published INDETERMINADO is informative: accept with a warning.
            return StockCheck(True, UNKNOWN_STOCK, warning=UNKNOWN_STOCK)
        if status == OUT_OF_STOCK:
            return StockCheck(False, "SKU_AGOTADO")
        return StockCheck(False, NOT_OBTAINED)

    def check_bulk(self, rows: list[dict], requested: list[str] | None = None) -> dict[str, StockCheck]:
        """Index by SKU; with `requested`, missing SKUs are "not obtained"."""
        checks = {
            str(row.get("sku")): self.check_row(row)
            for row in rows
            if isinstance(row, dict) and row.get("sku")
        }
        for sku in requested or []:
            checks.setdefault(sku, StockCheck(False, NOT_OBTAINED))
        return checks

    def check_quantity(self, verdict: str | None) -> StockCheck:
        """Quantity verdict: enough, short, undecided (warning) or not obtained."""
        if verdict == ENOUGH:
            return StockCheck(True, "OK")
        if verdict == SHORT:
            return StockCheck(False, "STOCK_INSUFICIENTE")
        if verdict == UNDECIDED:
            return StockCheck(True, UNDECIDED, warning=UNDECIDED)
        return StockCheck(False, NOT_OBTAINED)
