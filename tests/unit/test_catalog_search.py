"""Catalog search normalization (SPEC-06 Req. 1-2). No network, no database."""
from src.domain.services.product_normalizer import ProductNormalizer

CATEGORIES = [
    {"category_id": "cat-zapatillas", "name": "Zapatillas"},
    {"category_id": "cat-polos", "name": "Polos"},
    {"category_id": "cat-mochilas", "name": "Mochilas y morrales"},
]
BRANDS = [
    {"brand_id": "brand-nike", "name": "Nike"},
    {"brand_id": "brand-adidas", "name": "Adidas"},
]


def make_normalizer() -> ProductNormalizer:
    return ProductNormalizer(categories=CATEGORIES, brands=BRANDS)


def test_combined_filters_resolve_ids():
    filters = make_normalizer().normalize_filters(
        q="zapatillas", categoria="Zapatillas", marca="Nike"
    )
    assert filters.q == "zapatillas"
    assert filters.category_id == "cat-zapatillas"
    assert filters.brand_id == "brand-nike"
    assert filters.unresolved_category is None
    assert filters.unresolved_brand is None


def test_peruvian_synonym_resolves_category():
    assert (
        make_normalizer().normalize_category("chimpunes")["category_id"]
        == "cat-zapatillas"
    )


def test_misspelled_brand_resolves_by_similarity():
    assert make_normalizer().normalize_brand("nik")["brand_id"] == "brand-nike"


def test_unknown_brand_stays_unresolved_for_clarification():
    filters = make_normalizer().normalize_filters(marca="Inexistente")
    assert filters.brand_id is None
    assert filters.unresolved_brand == "Inexistente"


def test_suggest_lists_close_names():
    suggestions = make_normalizer().suggest("nik", BRANDS)
    assert suggestions and suggestions[0] == "Nike"


def test_chips_cap_at_six():
    filters = make_normalizer().normalize_filters(q="x", categoria="a", marca="b")
    assert len(filters.chips) <= 6
