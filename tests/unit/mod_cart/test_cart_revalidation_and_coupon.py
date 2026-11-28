"""Unit tests for batch stock revalidation and invalid coupon removal on GET /carrito (#247)."""
from __future__ import annotations

import uuid

import pytest

from src.modules.mod_cart.application.manage_cart_use_case import ManageCartUseCase
from src.shared.domain.caller_identity import CallerIdentity
from tests.fakes.fake_commerce import (
    FakeCarts,
    FakeCatalogPort,
    FakeConversationContext,
    FakeInventoryPort,
    FakeItems,
    FakeUow,
)

CUSTOMER_ID = uuid.uuid4()
SKU_OK = "NIK-REV7-42-NEG"
SKU_OOS = "ADI-ULT-41-BLA"
SKU_IND = "PUM-RUN-40-AZU"
SKU_MISSING = "UNKNOWN-SKU-999"


class _SpyInventory(FakeInventoryPort):
    def __init__(
        self, statuses: dict[str, str], *, default: str = "DISPONIBLE", fail: bool = False
    ) -> None:
        super().__init__(statuses, default=default)
        self.availability_calls: list[list[str]] = []
        self._fail = fail

    async def get_availability(self, skus, **kwargs):
        self.availability_calls.append(list(skus))
        if self._fail:
            raise RuntimeError("inventory unavailable")
        return await super().get_availability(skus, **kwargs)


class _CouponRejectedCatalog(FakeCatalogPort):
    def __init__(self, prices: dict[str, float], *, auto_discount: float = 0.0) -> None:
        super().__init__()
        self._custom_prices = dict(prices)
        self._auto_discount = auto_discount
        self.evaluate_calls: list[str | None] = []

    async def get_prices(self, skus):
        return [
            {"sku": sku, "currency": "PEN", "precio_regular": self._custom_prices[sku]}
            for sku in skus
            if sku in self._custom_prices
        ]

    async def evaluate_promotions(self, lines, *, coupon_code=None, customer_ref=None):
        self.evaluate_calls.append(coupon_code)
        total = sum(self._custom_prices.get(line["sku"], 0.0) * line["quantity"] for line in lines)
        if coupon_code:
            return {
                "currency": "PEN",
                "original_amount": f"{total:.2f}",
                "discount_amount": "0.00",
                "result_amount": f"{total:.2f}",
                "coupon_applied": False,
                "reason": "MINIMO_NO_ALCANZADO",
            }
        disc = min(self._auto_discount, total)
        return {
            "currency": "PEN",
            "original_amount": f"{total:.2f}",
            "discount_amount": f"{disc:.2f}",
            "result_amount": f"{total - disc:.2f}",
            "promotion_id": "promo-auto" if disc > 0 else None,
        }


@pytest.mark.asyncio
async def test_view_removes_invalid_coupon_reevaluates_and_matches_spec_message() -> None:
    carts, items, uow = FakeCarts(), FakeItems(), FakeUow()
    cart = await carts.crear_para_cliente(CUSTOMER_ID)
    await carts.set_coupon(cart.id, "MIN500")
    await items.agregar(
        cart_id=cart.id,
        product_id="p-1",
        sku=SKU_OK,
        name="Zapatilla Runner",
        quantity=1,
        unit_price_ref=120.0,
    )

    catalog = _CouponRejectedCatalog({SKU_OK: 120.0}, auto_discount=10.0)
    uc = ManageCartUseCase(
        carts,
        items,
        catalog,
        _SpyInventory({SKU_OK: "DISPONIBLE"}),
        FakeConversationContext(),
        uow,
    )

    view = await uc.view(CallerIdentity(customer_id=CUSTOMER_ID), None)
    datos = view["bloques"][0]["datos"]

    assert carts.rows[cart.id].coupon_code is None
    # Re-evaluated without coupon so automatic S/ 10 promo applies.
    assert catalog.evaluate_calls == ["MIN500", None]
    assert datos["total"] == pytest.approx(110.0)
    assert "Quitamos el cupón MIN500 porque tu compra ya no alcanza el mínimo" in datos["avisos"]
    assert uow.commits >= 1


@pytest.mark.asyncio
async def test_view_revalidates_stock_in_single_batch_precedence_and_indeterminado() -> None:
    carts, items, uow = FakeCarts(), FakeItems(), FakeUow()
    cart = await carts.crear_para_cliente(CUSTOMER_ID)
    await items.agregar(
        cart_id=cart.id,
        product_id="p-ok",
        sku=SKU_OK,
        name="Polo Activo",
        quantity=2,
        unit_price_ref=100.0,
    )
    await items.agregar(
        cart_id=cart.id,
        product_id="p-ind",
        sku=SKU_IND,
        name="Medias Runner",
        quantity=1,
        unit_price_ref=30.0,
    )
    await items.agregar(
        cart_id=cart.id,
        product_id="p-oos",
        sku=SKU_OOS,
        name="Short Agotado",
        quantity=1,
        unit_price_ref=80.0,
    )
    await items.agregar(
        cart_id=cart.id,
        product_id="p-miss",
        sku=SKU_MISSING,
        name="Gorra Descontinuada",
        quantity=1,
        unit_price_ref=50.0,
    )

    # SKU_MISSING also reports AGOTADO in inventory, but missing price MUST win -> "Ya no disponible".
    inv = _SpyInventory(
        {
            SKU_OK: "DISPONIBLE",
            SKU_IND: "INDETERMINADO",
            SKU_OOS: "AGOTADO",
            SKU_MISSING: "AGOTADO",
        }
    )
    catalog = _CouponRejectedCatalog({SKU_OK: 100.0, SKU_IND: 30.0, SKU_OOS: 80.0})
    uc = ManageCartUseCase(carts, items, catalog, inv, FakeConversationContext(), uow)

    view = await uc.view(CallerIdentity(customer_id=CUSTOMER_ID), None)
    datos = view["bloques"][0]["datos"]
    by_sku = {line["sku"]: line for line in datos["lineas"]}

    # Single batch call (no N+1)
    assert len(inv.availability_calls) == 1
    assert set(inv.availability_calls[0]) == {SKU_OK, SKU_IND, SKU_OOS, SKU_MISSING}

    # SKU_OK (200) + SKU_IND (30) count toward subtotal/total; AGOTADO & missing price excluded.
    assert datos["subtotal"] == pytest.approx(230.0)
    assert datos["total"] == pytest.approx(230.0)

    assert by_sku[SKU_OK]["disponible"] is True
    assert by_sku[SKU_IND]["disponible"] is True
    assert by_sku[SKU_IND]["nota"] == "No pudimos confirmar el stock"
    assert "No pudimos confirmar el stock" in datos["avisos"]

    assert by_sku[SKU_OOS]["disponible"] is False
    assert by_sku[SKU_OOS]["nota"] == "Agotado"

    # Precedence: inactive/missing price is NEVER shown as "Agotado".
    assert by_sku[SKU_MISSING]["disponible"] is False
    assert by_sku[SKU_MISSING]["nota"] == "Ya no disponible"

    assert carts.rows[cart.id].totals_snapshot["tiene_no_disponibles"] is True


@pytest.mark.asyncio
async def test_view_keeps_lines_with_notice_when_availability_service_fails() -> None:
    carts, items, uow = FakeCarts(), FakeItems(), FakeUow()
    cart = await carts.crear_para_cliente(CUSTOMER_ID)
    await items.agregar(
        cart_id=cart.id,
        product_id="p-ok",
        sku=SKU_OK,
        name="Polo Activo",
        quantity=1,
        unit_price_ref=100.0,
    )

    uc = ManageCartUseCase(
        carts,
        items,
        _CouponRejectedCatalog({SKU_OK: 100.0}),
        _SpyInventory({}, fail=True),
        FakeConversationContext(),
        uow,
    )
    view = await uc.view(CallerIdentity(customer_id=CUSTOMER_ID), None)
    datos = view["bloques"][0]["datos"]
    assert datos["lineas"][0]["disponible"] is True
    assert datos["lineas"][0]["nota"] == "No pudimos confirmar el stock"
    assert "No pudimos confirmar el stock" in datos["avisos"]
    assert datos["total"] == pytest.approx(100.0)
