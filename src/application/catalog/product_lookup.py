"""Shared product lookup over the catalog port (SPEC-06, SPEC-09).

Resolves carousel ordinals against the conversation working memory and
variant attributes against a product detail. Used by the catalog and
cart use cases so both resolve the same SKU for the same words.
"""
from src.domain.services.variant_resolver import VariantResolver, VariantMatch

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
