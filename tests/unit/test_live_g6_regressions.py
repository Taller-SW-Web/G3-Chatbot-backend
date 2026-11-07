"""Regressions for payloads observed during public G6 validation; no live calls."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from src.application.tools.action_dispatcher import ActionDispatcher
from src.application.tools.cart_handlers import build_cart_handlers
from src.application.tools.catalog_tools import AddArguments, SearchArguments, build_tool_registry
from src.application.tools.tool_registry import ExecutionContext
from src.application.use_cases import manage_catalog_use_case as catalog_module
from src.application.use_cases.manage_cart_use_case import CartError
from src.domain.services.product_projection import normalize_detail
from src.domain.services.variant_resolver import VariantResolver
from src.ports.outbound.module_errors import ModuleRequestError
from tests.unit.test_astra_regressions import cards, g6_detail, g6_service
from tests.unit.test_cart_use_case import (
    CONVERSATION_ID,
    CUSTOMER_ID,
    make_caller,
    make_cart_use_case,
    owned,
)


def supplier_detail():
    detail = g6_detail("prod-001", "nike", ("42", "43"))
    for row in detail["variants"]:
        row["identifying_attributes"][0]["valor"] = {"nombre": row["sku"].split("-")[-1], "valor_id": "size-id"}
        row["identifying_attributes"].append({"nombre": "Color", "valor": {"nombre": "Negro", "valor_id": "color-id"}})
    detail["variants"][0]["sku"] = "NIK-REV7-42-NEG"
    return detail


@pytest.mark.parametrize("value,expected", [
    (" 42 ", "42"), ({"nombre": "42", "valor_id": "id"}, "42"),
    ({"valor_id": "id"}, None), ({"nombre": {"nested": "42"}}, None),
    ({"nombre": " "}, None), (None, None), (42, None), ([], None),
])
def test_g6_attribute_values_remain_scalar_or_unknown(value, expected):
    detail = g6_detail("p", "b")
    detail["variants"][0]["identifying_attributes"][0]["valor"] = value
    normalized = normalize_detail(detail)
    assert normalized["variants"][0]["talla"] == expected
    assert normalize_detail(normalized) == normalized


def test_legacy_scalar_and_missing_attributes():
    result = normalize_detail({"variants": [{"sku": "s", "talla": "M", "color": "Azul"}, {"sku": "other"}]})
    assert result["variants"][0]["talla"] == "M"
    assert result["variants"][0]["color"] == "Azul"
    assert result["variants"][1]["talla"] is None


@pytest.mark.asyncio
async def test_supplier_object_values_resolve_and_filter_before_pricing():
    detail = supplier_detail()
    assert VariantResolver().resolve(detail, size="42", color="negro").sku == "NIK-REV7-42-NEG"
    uc, ctx = g6_service([detail])
    row = cards(await uc.search(SearchArguments(talla="42", color="negro"), ctx))[0]
    assert row["variants"] == [{"sku": "NIK-REV7-42-NEG", "talla": "42", "color": "Negro"}]
    uc._catalog.get_prices.assert_awaited_once_with(["NIK-REV7-42-NEG"])


@pytest.mark.parametrize("payload", [
    {}, {"sku": "s", "productoRef": 1}, {"product_id": "p", "productoRef": 1},
    {"sku": "s", "product_id": "p", "productoRef": 1},
    {"nombre": "n", "productoRef": 1}, {"nombre": "n", "sku": "s"},
    {"nombre": "n", "product_id": "p"},
])
def test_conflicting_product_references_stay_invalid(payload):
    with pytest.raises(ValueError):
        AddArguments(**payload)


def test_add_arguments_accept_a_product_name_alone():
    assert AddArguments(nombre="Nike Revolution 7").nombre == "Nike Revolution 7"


@pytest.mark.asyncio
async def test_exact_single_sku_ui_action_reaches_cart_and_cross_sell():
    cart, conversations = make_cart_use_case()
    owned(cart, conversations)
    cart._catalog.get_product_detail = AsyncMock(return_value=supplier_detail())
    cart._catalog.list_promotions = AsyncMock()
    cart._discovery = AsyncMock()
    cart._discovery.cross_sell.return_value = []
    dispatcher = ActionDispatcher(build_tool_registry(build_cart_handlers(cart)))
    context = ExecutionContext(cliente_id=str(CUSTOMER_ID), conversacion_id=str(CONVERSATION_ID))
    result = await dispatcher.despachar({"tipo": "AGREGAR_AL_CARRITO", "payload": {
        "sku": "NIK-REV7-42-NEG", "product_id": "prod-001"}}, context)
    assert result.code == "OK"
    line = result.data["bloques"][0]["datos"]["lineas"][0]
    assert line["sku"] == "NIK-REV7-42-NEG" and line["cantidad"] == 1
    cart._discovery.cross_sell.assert_awaited_once_with("prod-001", context)
    cart._catalog.list_promotions.assert_not_awaited()


@pytest.mark.parametrize("overrides", [
    {"product_id": "other"}, {"sku": "OTHER-SKU"}, {"size": "99"}, {"color": "Azul"},
])
@pytest.mark.asyncio
async def test_explicit_sku_context_is_verified_before_stock_or_mutation(overrides):
    cart, conversations = make_cart_use_case()
    owned(cart, conversations)
    cart._catalog.get_product_detail = AsyncMock(return_value=supplier_detail())
    cart._inventory.get_availability = AsyncMock()
    cart._items.agregar = AsyncMock()
    args = {"sku": "NIK-REV7-42-NEG", "product_id": "prod-001", **overrides}
    with pytest.raises(CartError) as error:
        await cart.add(make_caller(), CONVERSATION_ID, **args)
    assert error.value.code == "PRODUCTO_NO_DISPONIBLE"
    cart._inventory.get_availability.assert_not_awaited()
    cart._items.agregar.assert_not_awaited()


@pytest.mark.asyncio
async def test_prices_cover_every_sku_with_four_small_calls_at_most():
    uc, _ = g6_service([])
    active = peak = 0
    seen = []

    async def price(batch):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        seen.extend(batch)
        await asyncio.sleep(0)
        active -= 1
        return [{"sku": sku, "precio_regular": 1} for sku in batch]

    uc._catalog.get_prices = AsyncMock(side_effect=price)
    skus = [f"sku-{i}" for i in range(47)]
    result = await uc._prices_by_sku([*skus, skus[0]])
    assert set(result) == set(skus) and seen == skus
    assert peak == 4
    assert all(len(call.args[0]) <= 5 for call in uc._catalog.get_prices.await_args_list)


@pytest.mark.asyncio
async def test_rate_limit_stops_next_wave_and_preserves_retry_after():
    uc, _ = g6_service([])
    error = ModuleRequestError("productos", 429, "RATE_LIMIT_EXCEDIDO")
    error.retry_after = "30"

    async def price(batch):
        if batch[0] == "0":
            raise error
        return [{"sku": sku} for sku in batch]

    uc._catalog.get_prices = AsyncMock(side_effect=price)
    with pytest.raises(catalog_module.CatalogError) as raised:
        await uc._prices_by_sku([str(i) for i in range(30)])
    assert raised.value.status == 429 and raised.value.retry_after == "30"
    assert uc._catalog.get_prices.await_count == 4


@pytest.mark.asyncio
async def test_price_aggregate_budget_fails_without_partial_success(monkeypatch):
    uc, _ = g6_service([])
    monkeypatch.setattr(catalog_module, "COMMERCIAL_BUDGET_SECONDS", 0.01)

    async def price(batch):
        await asyncio.Event().wait()

    uc._catalog.get_prices = AsyncMock(side_effect=price)
    with pytest.raises(catalog_module.CatalogError) as raised:
        await uc._prices_by_sku(["sku"])
    assert raised.value.code == "SERVICIO_NO_DISPONIBLE"
