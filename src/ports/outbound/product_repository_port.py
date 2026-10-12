"""Outbound ports: Productos module catalog and inventory (H3-06, ADR-0017).

The chatbot never touches the Productos database. Everything goes through
these ports over HTTP; switching mock<->real is config-only (base URL).
`ProductsClient` implements `ProductCatalogPort`, `InventoryClient`
implements `InventoryPort`.
"""
from typing import Any, Protocol


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
