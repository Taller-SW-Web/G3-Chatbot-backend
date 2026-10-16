"""Outbound ports: Productos module catalog and inventory (ADR-0017).

The chatbot never touches the Productos database. Everything goes through
these ports over HTTP; switching mock<->real is config-only (base URL).
`ProductsClient` implements `ProductCatalogPort`, `InventoryClient`
implements `InventoryPort`.
"""
from typing import Any, Protocol


MAX_SKUS_PER_CALL = 20

# Commercial availability batches (Productos HTTP 0.5.0, SPEC-10): the
# projection endpoint accepts up to 100 SKUs per call; prices keep the
# shared 20-SKU cap above.
MAX_COMMERCIAL_SKUS = 100

# Deprecated Spanish-spelling alias (kept for backward compat, remove in Hito 4).
MAX_COMERCIAL_SKUS = MAX_COMMERCIAL_SKUS


def normalize_sku_list(skus: list[str], *, limit: int = MAX_SKUS_PER_CALL) -> list[str]:
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

    async def list_promotions(
        self, *, correlation_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Current promotions (`GET /promociones`, provisional shape)."""
        ...

    async def get_prices(
        self,
        skus: list[str],
        *,
        channel: str = "CHATBOT",
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Bulk prices (`GET /precios?skus=&canal=`, Productos HTTP 0.5.0)."""
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
        self,
        skus: list[str],
        *,
        channel: str = "CHATBOT",
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Commercial projection per SKU (`status`), one call, max 100 SKUs (SPEC-10)."""
        ...

    async def verify_quantities(
        self,
        lines: list[dict[str, Any]],
        *,
        channel_id: str = "CHATBOT",
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Non-binding per-line verdicts (`SUFICIENTE`, `INSUFICIENTE`,
        `INDETERMINADO`) for requested quantities (SPEC-10)."""
        ...

    async def get_sku_availability(
        self, sku: str, *, correlation_id: str | None = None
    ) -> dict[str, Any] | None:
        """Single-SKU convenience. None when the SKU is unknown to Productos."""
        ...
