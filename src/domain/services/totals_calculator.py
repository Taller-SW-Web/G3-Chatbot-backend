"""Cart totals over live Productos data (SPEC-11 Req. 3).

`subtotal` is regular price × quantity; `discounts` come from a single
promotions evaluation (`discount_amount` + `promotion_id`, no per-line
breakdown); `shipping` stays None until checkout quotes it. Inactive
lines never add to the total. `Decimal` throughout; rounding to two
decimals happens only when presenting.
"""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PricedLine:
    sku: str
    name: str
    quantity: int
    unit_price: Decimal
    previous_price: Decimal | None = None
    image_url: str | None = None
    variant_description: str | None = None
    available: bool = True


@dataclass(frozen=True)
class CartTotals:
    subtotal: Decimal
    discounts: tuple[dict, ...] = ()
    discount_total: Decimal = Decimal("0")
    shipping: Decimal | None = None
    total: Decimal = Decimal("0")
    notices: tuple[str, ...] = ()
    promotions_available: bool = True


def _money(value: Decimal | float | int | str) -> Decimal:
    return Decimal(str(value))


class TotalsCalculator:
    def calculate(
        self,
        lines: list[PricedLine],
        *,
        evaluation: dict | None = None,
        promotion_names: dict[str, str] | None = None,
        shipping: Decimal | float | int | None = None,
    ) -> CartTotals:
        active = [line for line in lines if line.available and line.quantity > 0]
        subtotal = sum((line.unit_price * line.quantity for line in active), Decimal("0"))
        notices: list[str] = []
        for line in active:
            if line.previous_price is not None and line.previous_price != line.unit_price:
                notices.append(
                    f"El precio de {line.name} cambió de "
                    f"S/ {line.previous_price:.2f} a S/ {line.unit_price:.2f}"
                )
        discounts: list[dict] = []
        discount_total = Decimal("0")
        promotions_available = True
        if evaluation is None:
            promotions_available = False
            notices.append("No pudimos calcular promociones en este momento")
        else:
            amount = evaluation.get("discount_amount") or 0
            discount_total = _money(amount)
            if discount_total > 0:
                promotion_id = evaluation.get("promotion_id")
                names = promotion_names or {}
                discounts.append({
                    "promotion_id": promotion_id,
                    "name": names.get(str(promotion_id), "Descuento")
                    if promotion_id else "Descuento",
                    "amount": discount_total,
                })
        shipping_cost = _money(shipping) if shipping is not None else None
        total = subtotal - discount_total + (shipping_cost or Decimal("0"))
        return CartTotals(
            subtotal=subtotal,
            discounts=tuple(discounts),
            discount_total=discount_total,
            shipping=shipping_cost,
            total=total,
            notices=tuple(notices),
            promotions_available=promotions_available,
        )

    def present(self, amount: Decimal) -> str:
        return f"S/ {amount:.2f}"
