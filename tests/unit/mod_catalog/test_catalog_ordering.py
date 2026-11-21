"""Catalog `orden` is real and page-scoped (SPEC-06 + product decision D7)."""
from decimal import Decimal

from src.modules.mod_catalog.domain.product_ordering import normalize_order, sort_summaries

ROWS = [
    {"name": "Zapatillas", "price": Decimal("299.90")},
    {"name": "Polos", "price": Decimal("59.90")},
    {"name": "Mochilas", "price": None},
    {"name": "Camisetas", "price": Decimal("89.90")},
]


def test_default_relevance_keeps_the_upstream_order():
    assert sort_summaries(ROWS, None) == ROWS
    assert sort_summaries(ROWS, "relevancia") == ROWS


def test_name_ascending_and_descending():
    assert [row["name"] for row in sort_summaries(ROWS, "nombre_asc")] == [
        "Camisetas", "Mochilas", "Polos", "Zapatillas"
    ]
    assert [row["name"] for row in sort_summaries(ROWS, "nombre_desc")] == [
        "Zapatillas", "Polos", "Mochilas", "Camisetas"
    ]


def test_price_ascending_and_descending_leave_missing_price_last():
    assert [row["name"] for row in sort_summaries(ROWS, "precio_asc")] == [
        "Polos", "Camisetas", "Zapatillas", "Mochilas"
    ]
    assert [row["name"] for row in sort_summaries(ROWS, "precio_desc")] == [
        "Zapatillas", "Camisetas", "Polos", "Mochilas"
    ]


def test_uppercase_aliases_still_work_for_the_llm_tool():
    assert normalize_order("PRECIO_ASC") == "PRECIO_ASC"
    assert normalize_order("precio_asc") == "PRECIO_ASC"
    assert normalize_order("cualquier_cosa") == "RELEVANCIA"
