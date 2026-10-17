"""Cart totals and line resolution (SPEC-11 Req. 2-3). No network, no database."""
from decimal import Decimal

from src.domain.services.cart_line_resolver import CartLineResolver
from src.domain.services.totals_calculator import PricedLine, TotalsCalculator

LINES = [
    PricedLine(sku="A", name="Medias running", quantity=1, unit_price=Decimal("49.90")),
    PricedLine(sku="B", name="Zapatillas X", quantity=2, unit_price=Decimal("289.90")),
]


def test_totals_add_regular_prices():
    totals = TotalsCalculator().calculate(LINES, evaluation={"discount_amount": 0})
    assert totals.subtotal == Decimal("629.70")
    assert totals.total == Decimal("629.70")
    assert totals.promotions_available is True


def test_single_evaluation_discount_applies_once():
    totals = TotalsCalculator().calculate(
        LINES,
        evaluation={"discount_amount": "62.97", "promotion_id": "promo-1"},
        promotion_names={"promo-1": "Cyber 10%"},
    )
    assert totals.discount_total == Decimal("62.97")
    assert totals.discounts[0]["name"] == "Cyber 10%"
    assert totals.total == Decimal("566.73")


def test_unknown_promotion_name_falls_back():
    totals = TotalsCalculator().calculate(
        LINES, evaluation={"discount_amount": 10, "promotion_id": "missing"}
    )
    assert totals.discounts[0]["name"] == "Descuento"


def test_missing_evaluation_warns_and_keeps_subtotal():
    totals = TotalsCalculator().calculate(LINES, evaluation=None)
    assert totals.promotions_available is False
    assert totals.total == totals.subtotal
    assert any("promociones" in notice for notice in totals.notices)


def test_price_change_notice_and_inactive_exclusion():
    lines = [
        PricedLine(sku="A", name="X", quantity=1, unit_price=Decimal("299.90"),
                   previous_price=Decimal("239.90")),
        PricedLine(sku="B", name="Y", quantity=1, unit_price=Decimal("10"),
                   available=False),
    ]
    totals = TotalsCalculator().calculate(lines, evaluation={"discount_amount": 0})
    assert totals.subtotal == Decimal("299.90")
    assert any("239.90" in notice and "299.90" in notice for notice in totals.notices)


def test_line_resolution_by_id_ref_and_name():
    rows = [
        {"item_id": "i1", "name": "Medias running"},
        {"item_id": "i2", "name": "Zapatillas X"},
        {"item_id": "i3", "name": "Zapatillas Y"},
    ]
    resolver = CartLineResolver()
    assert resolver.resolve(rows, item_id="i2").item_id == "i2"
    assert resolver.resolve(rows, item_id="missing").found is False
    assert resolver.resolve(rows, item_ref=1).item_id == "i1"
    assert resolver.resolve(rows, item_ref=9).found is False
    assert resolver.resolve(rows, name="medias").item_id == "i1"
    ambiguous = resolver.resolve(rows, name="zapatillas")
    assert ambiguous.found is False
    assert len(ambiguous.ambiguous) == 2
