"""Sellable base SKUs from captured public G6 payloads, without live calls."""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.adapters.inbound.http.catalog_router import create_catalog_router
from src.adapters.outbound.fakes.fake_products import FakeCatalogPort
from src.application.tools.action_dispatcher import ActionDispatcher
from src.application.tools.cart_handlers import build_cart_handlers
from src.application.tools.catalog_tools import (
    ProductArguments,
    RecommendationArguments,
    SearchArguments,
    build_tool_registry,
)
from src.application.tools.tool_registry import ExecutionContext
from src.application.use_cases import manage_catalog_use_case as catalog_module
from src.application.use_cases.manage_catalog_use_case import CatalogError
from src.domain.services.offer_policy import price_projection
from src.domain.services.product_projection import sellable_skus
from src.domain.services.variant_resolver import VariantResolver
from tests.unit.test_astra_regressions import cards, g6_service
from tests.unit.test_cart_use_case import CONVERSATION_ID, CUSTOMER_ID, make_cart_use_case, owned

FIXTURES = Path(__file__).parent / "fixtures" / "g6_no_variants"
SKU = "ADI-MESSI-JI8291-05"


def payload(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def detail():
    return payload("detail_ad478840-d9fa-5c02-8e4f-d6bdbd0b1914.json")


@pytest.fixture(autouse=True)
def commercial_clock(monkeypatch):
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    monkeypatch.setattr(catalog_module, "price_projection", lambda row: price_projection(row, now))


def service(data=None, status="DISPONIBLE"):
    data = detail() if data is None else data
    uc, ctx = g6_service([data])
    uc._catalog.get_prices = AsyncMock(return_value=payload("prices_0.json")["items"])
    uc._inventory.get_availability = AsyncMock(return_value=[{"sku": data.get("sku_base"), "status": status}])
    return uc, ctx


def assert_simple(row):
    assert row["single_sku"] == SKU
    assert row["has_variants"] is False
    assert row["variants"] == []
    assert row["price"] == Decimal("69.52")
    assert row["regular_price"] == "79.00"
    assert row["offer_price"] == "69.52"
    assert row["availability"] == "DISPONIBLE"


@pytest.mark.asyncio
async def test_search_real_base_sku_price_and_availability():
    uc, ctx = service()
    uc._inventory.get_availability.return_value = payload("avail_0.json")["items"]
    assert_simple(cards(await uc.search(SearchArguments(), ctx))[0])
    uc._catalog.get_prices.assert_awaited_once_with([SKU])
    uc._inventory.get_availability.assert_awaited_once_with([SKU])


@pytest.mark.asyncio
async def test_general_price_without_offer():
    data = {**detail(), "sku_base": "NIK-ACADEMY-HV4386-05", "name": "Nike Academy"}
    uc, ctx = service(data)
    row = cards(await uc.search(SearchArguments(), ctx))[0]
    assert row["price"] == Decimal("129.90")
    assert row["regular_price"] == "129.90" and row["offer_price"] is None


@pytest.mark.asyncio
async def test_detail_has_real_sku_without_synthetic_variant():
    uc, ctx = service()
    result = await uc.detail(ProductArguments(product_id=detail()["product_id"]), ctx)
    assert result["bloques"][0]["tipo"] == "DETALLE_PRODUCTO"
    assert_simple(result["bloques"][0]["datos"]["producto"])


@pytest.mark.parametrize("endpoint", ["/catalogo/productos", "/catalogo/productos/ad478840-d9fa-5c02-8e4f-d6bdbd0b1914"])
def test_rest_projects_simple_products(endpoint):
    uc, _ = service()
    app = FastAPI()
    app.include_router(create_catalog_router(lambda: uc))
    response = TestClient(app).get(endpoint)
    assert response.status_code == 200
    row = response.json()["items"][0] if endpoint.endswith("productos") else response.json()
    assert row["single_sku"] == SKU and row["variants"] == [] and row["has_variants"] is False
    assert Decimal(str(row["price"])) == Decimal("69.52")


@pytest.mark.parametrize("status,badge,text", [
    ("DISPONIBLE", "DISPONIBLE", "stock disponible"),
    ("STOCK_BAJO", "POCAS_UNIDADES", "pocas unidades"),
    ("AGOTADO", "AGOTADO", "agotado"),
    ("INDETERMINADO", "INDETERMINADO", "No puedo confirmar"),
])
@pytest.mark.asyncio
async def test_simple_availability_valid_states(status, badge, text):
    uc, ctx = service(status=status)
    row = cards(await uc.search(SearchArguments(), ctx))[0]
    assert row["availability"] == badge
    uc._inventory.get_availability.reset_mock()
    result = await uc.availability(ProductArguments(product_id=detail()["product_id"]), ctx)
    assert text in result["texto"]
    assert uc._inventory.get_availability.await_args_list[0].args == ([SKU],)


@pytest.mark.parametrize("overrides", [
    {"tiene_variantes": True, "sku_base": "NIK-REV7-BASE"},
    {"sku_base": "missing"}, {"sku_base": None}, {"sku_base": ""}, {"sku_base": "   "}, {"sku_base": 123},
])
@pytest.mark.asyncio
async def test_missing_or_unsellable_base_never_becomes_positive(overrides):
    data = {**detail(), **overrides}
    if data.get("sku_base") == "missing":
        data.pop("sku_base")
    uc, ctx = service(data)
    with pytest.raises(CatalogError):  # A page without any obtainable price is an error.
        await uc.search(SearchArguments(), ctx)
    assert VariantResolver().resolve(data).sku is None
    await uc.availability(ProductArguments(product_id=data["product_id"]), ctx)
    assert await uc._reference_price(data) is None
    uc._catalog.get_prices.assert_not_awaited()
    uc._inventory.get_availability.assert_not_awaited()


@pytest.mark.parametrize("filters", [{"talla": "5"}, {"color": "Multicolor"}, {"talla": "5", "color": "Multicolor"}])
@pytest.mark.asyncio
async def test_simple_product_cannot_match_unknown_attributes(filters):
    uc, ctx = service()
    assert cards(await uc.search(SearchArguments(**filters), ctx)) == []
    uc._catalog.get_prices.assert_not_awaited()
    uc._inventory.get_availability.assert_not_awaited()


@pytest.mark.asyncio
async def test_recommendations_and_reference_price_use_base_sku():
    uc, ctx = service()
    assert_simple(cards(await uc.recommend(RecommendationArguments(actividad="running"), ctx))[0])
    assert await uc._reference_price(detail()) == Decimal("69.52")
    assert cards(await uc.recommend(RecommendationArguments(actividad="running", talla="5"), ctx)) == []


@pytest.mark.asyncio
async def test_cross_sell_uses_base_sku():
    uc, ctx = service()
    uc._catalog.get_recommendations = AsyncMock(return_value=[{"product_id": detail()["product_id"], "type": "CROSS_SELL"}])
    blocks = await uc.cross_sell("other-product", ctx)
    assert_simple(blocks[0]["datos"]["productos"][0])


@pytest.mark.parametrize("has_variants,expected", [(False, "OK"), (True, "PRODUCTO_NO_DISPONIBLE")])
@pytest.mark.asyncio
async def test_cart_action_base_sku_requires_non_variant_product(has_variants, expected):
    data = {**detail(), "tiene_variantes": has_variants}
    sku = "NIK-REV7-BASE" if has_variants else SKU
    data["sku_base"] = sku
    cart, conversations = make_cart_use_case()
    owned(cart, conversations)
    cart._catalog.get_product_detail = AsyncMock(return_value=data)
    cart._catalog.get_price = AsyncMock(return_value={**payload("prices_0.json")["items"][0], "sku": sku})
    cart._inventory.get_availability = AsyncMock(return_value=[{"sku": sku, "status": "DISPONIBLE"}])
    dispatcher = ActionDispatcher(build_tool_registry(build_cart_handlers(cart)))
    context = ExecutionContext(cliente_id=str(CUSTOMER_ID), conversacion_id=str(CONVERSATION_ID))
    result = await dispatcher.despachar({"tipo": "AGREGAR_AL_CARRITO", "payload": {
        "sku": sku, "product_id": data["product_id"]}}, context)
    assert result.code == expected
    if has_variants:
        cart._inventory.get_availability.assert_not_awaited()
    else:
        assert result.data["bloques"][0]["datos"]["lineas"][0]["sku"] == SKU


@pytest.mark.asyncio
async def test_fake_simple_detail_keeps_seed_sku_and_no_variants():
    data = detail()
    fake = FakeCatalogPort(products=[data], details={}, prices={})
    projected = await fake.get_product_detail(data["product_id"])
    assert projected["sku_base"] == SKU and projected["variants"] == []
    assert projected["tiene_variantes"] is False


@pytest.mark.parametrize("flag", [True, False, None])
def test_variant_skus_always_take_priority_over_base(flag):
    data = {**detail(), "tiene_variantes": flag, "variants": [{"sku": "REAL-VARIANT"}]}
    assert sellable_skus(data) == ["REAL-VARIANT"]
    assert sellable_skus({**data, "variants": [{}]}) == []


@pytest.mark.asyncio
async def test_similar_products_include_stocked_base_sku_candidate():
    uc, _ = service()
    data = detail()
    uc._catalog.search_products.return_value = {"items": [data], "total": 1}
    reference = {**data, "product_id": "other"}
    assert await uc._find_similar(reference, Decimal("70")) == [
        {"product_id": data["product_id"], "name": data["name"]}]
    uc._catalog.get_prices.assert_awaited_once_with([SKU])
    uc._inventory.get_availability.assert_awaited_once_with([SKU])


@pytest.mark.asyncio
async def test_fake_seed_products_without_variants_keep_their_base_sku():
    fake = FakeCatalogPort()
    for product in fake._products:
        if product.get("tiene_variantes") is not False:
            continue
        data = await fake.get_product_detail(product["product_id"])
        assert data["sku_base"] == product["sku_base"]
        assert data["variants"] == [] and data["tiene_variantes"] is False
        assert sellable_skus(data) == [product["sku_base"]]
        prices = await fake.get_prices(sellable_skus(data))
        assert prices[0]["sku"] == product["sku_base"]
