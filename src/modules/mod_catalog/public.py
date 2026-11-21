"""Public surface of `mod-catalog`.

`mod-cart` (and `mod-chat` through the tool registry) consume the Productos
ports, the catalog use case and the pure product rules from here. Anything
else in `domain/`, `application/` or `adapters/` is private to the module.
"""
from src.modules.mod_catalog.application.discovery import fold
from src.modules.mod_catalog.application.manage_catalog_use_case import (
    CatalogError,
    ManageCatalogUseCase,
    OptionCache,
)
from src.modules.mod_catalog.application.product_lookup import (
    NameLookup,
    find_product_by_name,
    resolve_product_id,
    resolve_variant_sku,
)
from src.modules.mod_catalog.application.tools import (
    EmptyArguments,
    OrderQueryArguments,
    ProductArguments,
    RecommendationArguments,
    SearchArguments,
    build_tool_registry,
)
from src.modules.mod_catalog.domain.offer_policy import (
    amount,
    instant,
    price_projection,
    promotion_summaries,
)
from src.modules.mod_catalog.domain.product_projection import normalize_detail, sellable_skus
from src.modules.mod_catalog.domain.stock_validator import StockValidator
from src.modules.mod_catalog.domain.variant_resolver import (
    VariantMatch,
    VariantResolver,
    known_selection,
)
from src.modules.mod_catalog.module import (
    provide_catalog_service,
    provide_inventory_client,
    provide_product_catalog,
    provide_product_inventory,
    provide_products_client,
)

__all__ = [
    "CatalogError",
    "EmptyArguments",
    "ManageCatalogUseCase",
    "NameLookup",
    "OrderQueryArguments",
    "OptionCache",
    "ProductArguments",
    "RecommendationArguments",
    "SearchArguments",
    "StockValidator",
    "VariantMatch",
    "VariantResolver",
    "amount",
    "build_tool_registry",
    "find_product_by_name",
    "fold",
    "instant",
    "known_selection",
    "normalize_detail",
    "price_projection",
    "promotion_summaries",
    "provide_catalog_service",
    "provide_inventory_client",
    "provide_product_catalog",
    "provide_product_inventory",
    "provide_products_client",
    "resolve_product_id",
    "resolve_variant_sku",
    "sellable_skus",
]
