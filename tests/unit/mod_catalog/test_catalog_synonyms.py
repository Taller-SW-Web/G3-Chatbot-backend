"""Peruvian synonyms resolve to real catalog categories (SPEC-06 Req. 2).

Two option sets: the simplified fake catalog used by `PRODUCTS_MODE=fake`
and the seeded G6 taxonomy. The dictionary prefers the G6 name and falls
back to the fake name, never inventing a category that is not present.
"""
import pytest

from src.modules.mod_catalog.domain.product_normalizer import (
    DEFAULT_SYNONYMS,
    ProductNormalizer,
)

FAKE_CATEGORIES = [
    {"category_id": "cat-zapatillas", "name": "Zapatillas"},
    {"category_id": "cat-polos", "name": "Polos"},
    {"category_id": "cat-mochilas", "name": "Mochilas y morrales"},
]

# Category names seeded in G6 (scripts/seed/mcp/taxonomy de
# G6-Productos-Ofertas-backend): roots Calzado, Ropa, Equipamiento y
# Accesorios; existing level-2 Fútbol and Running.
G6_CATEGORIES = [
    {"category_id": "cat-calzado", "name": "Calzado"},
    {"category_id": "cat-futbol", "name": "Fútbol"},
    {"category_id": "cat-running", "name": "Running"},
    {"category_id": "cat-ropa", "name": "Ropa"},
    {"category_id": "cat-equipamiento", "name": "Equipamiento y Accesorios"},
]


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("chimpunes", "cat-zapatillas"),
        ("chimpun", "cat-zapatillas"),
        ("tabas", "cat-zapatillas"),
        ("tachos", "cat-zapatillas"),
        ("tacho", "cat-zapatillas"),
        ("fulbito", "cat-zapatillas"),
        ("zapatillas de fulbito", "cat-zapatillas"),
        ("buzo", "cat-polos"),
        ("buzos", "cat-polos"),
        ("polo", "cat-polos"),
        ("remera", "cat-polos"),
        ("camiseta", "cat-polos"),
        ("mochila", "cat-mochilas"),
        ("morral", "cat-mochilas"),
        ("canguro", "cat-mochilas"),
    ],
)
def test_alias_resolves_the_real_fake_category(alias, expected):
    normalizer = ProductNormalizer(categories=FAKE_CATEGORIES)

    assert normalizer.normalize_category(alias)["category_id"] == expected


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("zapatilla", "cat-calzado"),
        ("zapas", "cat-calzado"),
        ("chimpunes", "cat-futbol"),
        ("tachos", "cat-futbol"),
        ("zapatillas de fulbito", "cat-futbol"),
        ("buzo", "cat-ropa"),
        ("polo", "cat-ropa"),
        ("mochila", "cat-equipamiento"),
    ],
)
def test_alias_aligns_with_the_real_g6_category(alias, expected):
    normalizer = ProductNormalizer(categories=G6_CATEGORIES)

    assert normalizer.normalize_category(alias)["category_id"] == expected


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("CHIMPUNES", "cat-zapatillas"),
        ("Tachos", "cat-zapatillas"),
        ("ZAPATILLAS DE FULBITO", "cat-zapatillas"),
        ("Zapatillas de Fúlbito", "cat-zapatillas"),
        ("BÚZO", "cat-polos"),
        ("MÓCHILA", "cat-mochilas"),
    ],
)
def test_alias_is_case_and_accent_insensitive(alias, expected):
    normalizer = ProductNormalizer(categories=FAKE_CATEGORIES)

    assert normalizer.normalize_category(alias)["category_id"] == expected


@pytest.mark.parametrize(
    ("query", "expected_q", "expected_category"),
    [
        ("zapatillas de fulbito", None, "cat-zapatillas"),
        ("zapatillas de fulbito negras", "negras", "cat-zapatillas"),
        ("buzo adidas", "adidas", "cat-polos"),
        ("mochilas y morrales", None, "cat-mochilas"),
    ],
)
def test_multi_word_alias_is_tested_before_its_tokens(
    query, expected_q, expected_category
):
    normalizer = ProductNormalizer(categories=FAKE_CATEGORIES)

    filters = normalizer.normalize_filters(q=query)

    assert filters.category_id == expected_category
    assert filters.q == expected_q


def test_multi_word_alias_wins_over_generic_footwear():
    normalizer = ProductNormalizer(categories=G6_CATEGORIES)

    filters = normalizer.normalize_filters(q="zapatillas de fulbito negras")

    assert filters.category_id == "cat-futbol"
    assert filters.q == "negras"


def test_synonyms_never_invent_a_category_absent_from_the_catalog():
    normalizer = ProductNormalizer(
        categories=[{"category_id": "cat-polos", "name": "Polos"}]
    )

    assert normalizer.normalize_category("chimpunes") is None
    assert normalizer.normalize_category("buzo")["category_id"] == "cat-polos"


def test_default_synonyms_include_the_issue_terms():
    assert {"tacho", "tachos", "buzo", "buzos", "fulbito", "zapatillas de fulbito"} <= set(
        DEFAULT_SYNONYMS
    )
