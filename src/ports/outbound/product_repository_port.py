"""Outbound ports: Productos module catalog and inventory (H3-06, ADR-0017).

The chatbot never touches the Productos database. Everything goes through
these ports over HTTP; switching mock<->real is config-only (base URL).
`ProductsClient` implements `ProductCatalogPort`, `InventoryClient`
implements `InventoryPort`.
"""
from typing import Any, Protocol


class ModuleRequestError(Exception):
    """A 4xx answer from a module: the request was rejected, not a data row.

    Carries the HTTP `status` and the contract `code` (`codigo` normalized)
    so agent tools can tell "not found / not available" apart from valid
    results (SPEC-09, SPEC-10). Only 2xx bodies are returned as data.
    """

    def __init__(self, module: str, status: int, code: str, detail: str = ""):
        self.module = module
        self.status = status
        self.code = code
        self.detail = detail
        message = f"{module} request failed: HTTP {status} {code}"
        super().__init__(f"{message}: {detail}" if detail else message)


def parse_product_body(module: str, status_code: int, body: Any) -> Any:
    """Single checkpoint: return 2xx bodies, raise on 4xx.

    Reads `code` (or legacy `codigo`) from the body; unknown shapes fall
    back to `HTTP_{status_code}`. 5xx never reaches here: the base client
    already maps it to `ModuleUnavailableError`.
    """
    if 200 <= status_code < 300:
        return body
    code = f"HTTP_{status_code}"
    if isinstance(body, dict):
        code = str(body.get("code", body.get("codigo", code)))
    raise ModuleRequestError(module, status_code, code)


def normalize_sku_list(skus: list[str], *, limit: int = 20) -> list[str]:
    """Clean shared SKU input: strip, drop blanks/duplicates, enforce the cap.

    Commas are rejected (comma is the wire separator), so `["A,B"]` cannot
    smuggle two SKUs past the limit, and `["A"] * 20` counts as one.
    """
    if not skus:
        raise ValueError("skus must not be empty")
    clean: list[str] = []
    for sku in skus:
        item = sku.strip() if isinstance(sku, str) else ""
        if not item:
            raise ValueError(f"skus must not contain blanks, got {sku!r}")
        if "," in item:
            raise ValueError(f"sku must not contain a comma, got {item!r}")
        if item not in clean:
            clean.append(item)
    if len(clean) > limit:
        raise ValueError(f"max {limit} SKUs per call, got {len(clean)}")
    return clean


class ProductCatalogPort(Protocol):
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
        """Paginated container `{items, pagina, tamanio, total}` (SPEC-06)."""
        ...

    async def get_product_detail(
        self, product_id: str, *, correlation_id: str | None = None
    ) -> dict[str, Any]:
        """Product with variants, channel prices and per-SKU availability (SPEC-09)."""
        ...

    async def list_categories(
        self, *, correlation_id: str | None = None
    ) -> list[dict[str, Any]]:
        ...

    async def list_brands(
        self, *, correlation_id: str | None = None
    ) -> list[dict[str, Any]]:
        ...

    async def get_price(
        self, sku: str, *, correlation_id: str | None = None
    ) -> dict[str, Any]:
        """Price of one SKU on the channel (`GET /precios/skus/{sku}`)."""
        ...

    async def get_prices(
        self,
        skus: list[str],
        *,
        channel: str = "CHATBOT",
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Bulk prices (`GET /precios?skus=&channel_id=`)."""
        ...

    async def evaluate_promotions(
        self,
        lines: list[dict[str, Any]],
        *,
        channel_id: str = "CHATBOT",
        coupon_code: str | None = None,
        customer_ref: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Cart totals via `POST /promociones/evaluar` (SPEC-11)."""
        ...


class InventoryPort(Protocol):
    async def get_availability(
        self, skus: list[str], *, correlation_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Live availability per SKU, one call, max 20 SKUs (SPEC-10)."""
        ...

    async def get_sku_availability(
        self, sku: str, *, correlation_id: str | None = None
    ) -> dict[str, Any] | None:
        """Single-SKU convenience. None when the SKU is unknown to Productos."""
        ...
