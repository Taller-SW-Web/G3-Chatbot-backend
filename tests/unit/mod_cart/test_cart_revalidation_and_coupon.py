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
SKU_MISSING = "UNKNOWN-SKU-999"


class _SpyInventory(FakeInventoryPort):
    def __init__(self, statuses: dict[str, str], *, default: str = "DISPONIBLE") -> None:
        super().__init__(statuses, default=default)
        self.availability_calls: list[list[str]] = []

    async def get_availability(self, skus, **kwargs):
        self.availability_calls.append(list(skus))
        return await super().get_availability(skus, **kwargs)


class _CouponRejectedCatalog(FakeCatalogPort):
    def __init__(self, prices: dict[str, float]) -> None:
        super().__init__()
        self._custom_prices = dict(prices)

    async def get_prices(self, skus):
        return [
            {"sku": sku, "currency": "PEN", "precio_regular": self._custom_prices[sku]}
            for sku in skus
            if sku in self._custom_prices
        ]

    async def evaluate_promotions(self, lines, *, coupon_code=None, customer_ref=None):
        total = sum(self._custom_prices.get(line["sku"], 0.0) * line["quantity"] for line in lines)
        return {
            "currency": "PEN",
            "original_amount": f"{total:.2f}",
            "discount_amount": "0.00",
            "result_amount": f"{total:.2f}",
            "coupon_applied": False if coupon_code else None,
            "reason": "MINIMO_NO_ALCANZADO" if coupon_code else None,
        }


@pytest.mark.asyncio
async def test_view_removes_invalid_coupon_adds_notice_and_recalculates_total() -> None:
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

    uc = ManageCartUseCase(
        carts,
        items,
        _CouponRejectedCatalog({SKU_OK: 120.0}),
        _SpyInventory({SKU_OK: "DISPONIBLE"}),
        FakeConversationContext(),
        uow,
    )

    view = await uc.view(CallerIdentity(customer_id=CUSTOMER_ID), None)
    datos = view["bloques"][0]["datos"]

    assert carts.rows[cart.id].coupon_code is None
    assert datos["total"] == pytest.approx(120.0)
    assert any("MIN500" in aviso and "retirado" in aviso for aviso in datos["avisos"])
    assert uow.commits >= 1


@pytest.mark.asyncio
async def test_view_revalidates_stock_in_single_batch_and_separates_agotado_vs_unavailable() -> None:
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

    inv = _SpyInventory({SKU_OK: "DISPONIBLE", SKU_OOS: "AGOTADO", SKU_MISSING: "DISPONIBLE"})
    catalog = _CouponRejectedCatalog({SKU_OK: 100.0, SKU_OOS: 80.0})  # SKU_MISSING has no price
    uc = ManageCartUseCase(carts, items, catalog, inv, FakeConversationContext(), uow)

    view = await uc.view(CallerIdentity(customer_id=CUSTOMER_ID), None)
    datos = view["bloques"][0]["datos"]
    by_sku = {line["sku"]: line for line in datos["lineas"]}

    # Single batch call (no N+1)
    assert len(inv.availability_calls) == 1
    assert set(inv.availability_calls[0]) == {SKU_OK, SKU_OOS, SKU_MISSING}

    # Only SKU_OK (2 x 100 = 200) counts toward subtotal/total
    assert datos["subtotal"] == pytest.approx(200.0)
    assert datos["total"] == pytest.approx(200.0)

    # Distinct labels for AGOTADO vs price/identification error
    assert by_sku[SKU_OK]["disponible"] is True
    assert by_sku[SKU_OOS]["disponible"] is False
    assert by_sku[SKU_OOS]["nota"] == "Agotado"
    assert by_sku[SKU_MISSING]["disponible"] is False
    assert by_sku[SKU_MISSING]["nota"] == "Ya no disponible"

    # Totals snapshot flags unavailable items for checkout
    assert carts.rows[cart.id].totals_snapshot["tiene_no_disponibles"] is True
