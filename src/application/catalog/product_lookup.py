"""Shared product lookup over the catalog port (SPEC-06, SPEC-09).

Resolves carousel ordinals against the conversation working memory and
variant attributes against a product detail. Used by the catalog and
cart use cases so both resolve the same SKU for the same words.
"""
from dataclasses import dataclass
from typing import Any

from src.application.catalog.discovery import fold
from src.domain.services.variant_resolver import VariantMatch, VariantResolver

MEMORY_CAROUSEL_KEY = "ultimoCarrusel"
MEMORY_FILTERS_KEY = "filtrosVigentes"


def resolve_product_id(
    memory: dict,
    *,
    producto_ref: int | None = None,
    product_id: str | None = None,
) -> str | None:
    """Direct id wins; otherwise the 1-based carousel ordinal of this chat."""
    if product_id:
        return product_id
    if producto_ref is None:
        return None
    carousel = memory.get(MEMORY_CAROUSEL_KEY) or []
    if 1 <= producto_ref <= len(carousel):
        return str(carousel[producto_ref - 1])
    return None


def resolve_variant_sku(
    detail: dict,
    resolver: VariantResolver | None = None,
    *,
    size: str | None = None,
    color: str | None = None,
) -> VariantMatch:
    return (resolver or VariantResolver()).resolve(detail, size=size, color=color)


@dataclass(frozen=True)
class NameLookup:
    """Outcome of resolving a product name: one product id, or why there is none."""

    product_id: str | None
    found: bool
    items: tuple[dict[str, Any], ...] = ()


async def find_product_by_name(catalog, name: str) -> NameLookup:
    """Resolve a spoken product name to a product id over the catalog port.

    Exact (accent-insensitive) name wins; otherwise a single candidate whose name is
    contained in the request (or the reverse); otherwise the caller gets the candidates.
    Catalog errors propagate so each use case maps them to its own error type.
    """
    page = await catalog.search_products(q=name, size=5)
    items = tuple(page.get("items", []))
    if not items:
        return NameLookup(None, False)
    wanted = fold(name).strip()
    exact = [row for row in items if fold(str(row.get("name", ""))).strip() == wanted]
    related = [row for row in items if wanted and (
        fold(str(row.get("name", ""))) in wanted or wanted in fold(str(row.get("name", ""))))]
    for candidates in (exact, related, items):
        if len(candidates) == 1:
            return NameLookup(str(candidates[0]["product_id"]), True, items)
    return NameLookup(None, True, items)
