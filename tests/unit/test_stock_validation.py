"""Stock validation and similar products (SPEC-10). No network, no database."""
from decimal import Decimal

from src.domain.services.similar_products import SimilarProducts
from src.domain.services.stock_validator import StockValidator

ROWS = [
    {"sku": "A", "status": "DISPONIBLE"},
    {"sku": "B", "status": "STOCK_BAJO"},
    {"sku": "C", "status": "AGOTADO"},
    {"sku": "D", "status": "INDETERMINADO"},
    {"sku": "E", "status": "RARO"},
]


def test_projection_statuses_map_to_checks():
    validator = StockValidator()
    assert validator.check_row(ROWS[0]).ok is True
    assert validator.check_row(ROWS[1]).warning == "STOCK_BAJO"
    assert validator.check_row(ROWS[2]).code == "SKU_AGOTADO"
    assert validator.check_row(ROWS[3]).warning == "INDETERMINADO"
    assert validator.check_row(ROWS[4]).code == "INDETERMINADO"


def test_bulk_check_indexes_by_sku():
    checks = StockValidator().check_bulk(ROWS)
    assert checks["A"].ok is True
    assert checks["C"].ok is False


def test_quantity_verdicts():
    validator = StockValidator()
    assert validator.check_quantity("SUFICIENTE").ok is True
    assert validator.check_quantity("INSUFICIENTE").code == "STOCK_INSUFICIENTE"
    assert validator.check_quantity("INDETERMINADO").warning == "INDETERMINADO"
    assert validator.check_quantity(None).warning == "INDETERMINADO"


def test_similar_products_same_category_within_20_percent():
    candidates = [
        {"product_id": "p1", "category_id": "cat", "precio_regular": 100.0},
        {"product_id": "p2", "category_id": "cat", "precio_regular": 115.0},
        {"product_id": "p3", "category_id": "cat", "precio_regular": 200.0},
        {"product_id": "p4", "category_id": "other", "precio_regular": 100.0},
    ]
    found = SimilarProducts().find("cat", Decimal("100"), candidates, exclude={"p1"})
    assert [item["product_id"] for item in found] == ["p2"]
