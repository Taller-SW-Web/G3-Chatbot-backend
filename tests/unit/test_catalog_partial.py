"""Partial G6 catalog reads: captured wire shapes and explicit contract mocks."""
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.adapters.inbound.http.catalog_router import create_catalog_router
from src.adapters.inbound.http.problem import register_problem_handlers
from src.adapters.outbound.http_clients.inventory_client import InventoryClient
from src.adapters.outbound.http_clients.products_client import ProductsClient
from src.application.catalog import discovery
from src.application.tools.catalog_handlers import build_catalog_handlers
from src.application.tools.catalog_tools import (
    AvailabilityArguments,
    PromotionArguments,
    RecommendationArguments,
    SearchArguments,
)
from src.application.use_cases import manage_catalog_use_case as module
from src.application.use_cases.manage_catalog_use_case import CatalogError
from src.ports.outbound.module_errors import ModuleRequestError, ModuleUnavailableError
from tests.unit.test_discovery import PROMO, service
from tests.unit.test_live_g6_no_variants import SKU, payload
from tests.unit.test_live_g6_no_variants import service as real_service


def rows(result):
    return next(b["datos"]["productos"] for b in result["bloques"] if b["tipo"] == "CARRUSEL_PRODUCTOS")


@pytest.mark.asyncio
async def test_price_batch_failure_preserves_other_batch_and_rest_partial(monkeypatch):
    uc, ctx = service()
    original = uc._catalog.get_prices

    async def prices(skus):
        if "SKU0" in skus:
            raise ModuleUnavailableError("productos")
        return await original(skus)

    uc._catalog.get_prices = prices
    result = await uc.search(SearchArguments(), ctx)
    assert result["partial"] and result["bloques"][0]["datos"]["partial"]
    assert rows(result)[0]["missing_data"] == ["price"]
    assert rows(result)[0]["price"] is None and rows(result)[0]["offer_price"] is None
    assert rows(result)[0]["price_text"] == "Precio no disponible"
    assert rows(result)[-1]["price"] is not None
    app = FastAPI()
    app.include_router(create_catalog_router(lambda: uc))
    with TestClient(app) as client:
        assert client.get("/catalogo/productos").json()["partial"] is True
    offers = await uc.search(SearchArguments(soloOfertas=True), ctx)
    assert all(row["price"] is not None for row in rows(offers))
    assert offers["partial"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 422, 429])
async def test_all_price_batches_fail_and_429_stops_waves(monkeypatch, status):
    uc, ctx = service()
    monkeypatch.setattr(module, "PRICE_BATCH_SIZE", 1)
    monkeypatch.setattr(module, "PRICE_CONCURRENCY", 2)
    error = ModuleRequestError("productos", status, "UPSTREAM")
    error.retry_after = "7"
    uc._catalog.get_prices = AsyncMock(side_effect=error)
    with pytest.raises(CatalogError) as caught:
        await uc.search(SearchArguments(), ctx)
    assert caught.value.status == status
    if status == 429:
        assert uc._catalog.get_prices.await_count == 2
        assert caught.value.retry_after == "7"
        app = FastAPI()
        register_problem_handlers(app)
        app.include_router(create_catalog_router(lambda: uc))
        with TestClient(app) as client:
            response = client.get("/catalogo/productos")
            assert response.status_code == 429 and response.headers["Retry-After"] == "7"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [[], [{"sku": SKU.lower(), "status": "DISPONIBLE"}],
    [{"sku": SKU + "-X", "status": "DISPONIBLE"}], [{"sku": SKU, "status": "UNKNOWN"}],
    [None, {"sku": SKU}], ModuleUnavailableError("productos")])
async def test_availability_missing_never_becomes_positive(bad):
    uc, ctx = real_service()
    uc._inventory.get_availability = AsyncMock(**({"side_effect": bad} if isinstance(bad, Exception) else {"return_value": bad}))
    result = await uc.search(SearchArguments(), ctx)
    row = rows(result)[0]
    assert row["availability"] == "INDETERMINADO" and row["missing_data"] == ["availability"]
    assert result["partial"]


@pytest.mark.asyncio
async def test_exact_valid_indeterminate_is_obtained_and_bad_neighbor_isolated():
    uc, ctx = service()
    uc._inventory.get_availability = AsyncMock(return_value=[
        {"sku": "SKU0", "status": "INDETERMINADO"}, {"sku": "SKU1", "status": "DISPONIBLE"},
        {"sku": "SKU2", "status": "wrong"}, None])
    result = await uc.search(SearchArguments(), ctx)
    assert rows(result)[0]["missing_data"] == []
    assert rows(result)[1]["availability"] == "DISPONIBLE"
    assert rows(result)[2]["missing_data"] == ["availability"]


@pytest.mark.asyncio
async def test_inventory_client_keeps_valid_neighbor_from_real_envelope():
    body = payload("avail_0.json")
    body["items"].append(None)
    async with httpx.AsyncClient(base_url="http://g6", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as client:
        inventory = InventoryClient("http://g6", client=client)
        result = await inventory.get_availability([SKU])
    assert result[0]["status"] == "DISPONIBLE" and all(isinstance(row, dict) for row in result)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ModuleUnavailableError("productos"), TimeoutError()])
async def test_detail_failure_omitted_and_all_fail_error(error):
    uc, ctx = service()
    original = uc._catalog.get_product_detail

    async def detail(pid):
        if pid == "0":
            raise error
        return await original(pid)

    uc._catalog.get_product_detail = detail
    result = await uc.search(SearchArguments(), ctx)
    assert result["partial"] and len(rows(result)) == 7
    uc._catalog.get_product_detail = AsyncMock(side_effect=error)
    with pytest.raises(CatalogError):
        await uc.search(SearchArguments(), ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("promos_fail,products_fail", [(True, False), (False, True), (True, True)])
async def test_offers_independent_branches(promos_fail, products_fail):
    uc, ctx = service()
    uc._catalog.list_promotions = AsyncMock(side_effect=ModuleUnavailableError("productos")) if promos_fail else AsyncMock(return_value=[PROMO])
    if products_fail:
        uc._catalog.search_products = AsyncMock(side_effect=ModuleUnavailableError("productos"))
    if promos_fail and products_fail:
        with pytest.raises(CatalogError):
            await uc.offers(PromotionArguments(), ctx)
        return
    result = await uc.offers(PromotionArguments(), ctx)
    assert result["partial"]
    assert result["bloques"][0]["datos"]["unavailable"] == promos_fail
    assert "No pudimos consultar" in result["texto"]
    if products_fail:
        assert result["complete"] is False and len(result["bloques"]) == 1
    else:
        assert rows(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "deadline"])
async def test_offers_preserve_valid_page_when_next_page_fails_or_total_budget_expires(monkeypatch, failure):
    uc, ctx = service()
    original = uc._catalog.search_products
    uc._catalog.list_promotions = AsyncMock(return_value=[PROMO])
    monkeypatch.setattr(discovery, "OFFERS_BUDGET_SECONDS", 0.4)

    async def search(**kwargs):
        if kwargs.get("page", 1) == 2:
            if failure == "deadline":
                await asyncio.sleep(1)
            raise ModuleUnavailableError("productos")
        page = await original(**kwargs)
        return {**page, "total": 20}

    uc._catalog.search_products = search
    result = await uc.offers(PromotionArguments(), ctx)
    assert result["complete"] is False and result["partial"]
    assert result["pages_scanned"] == 1 and result["total_scope"] == "scanned_pages"
    assert rows(result) and "puede haber más ofertas" in result["texto"]


@pytest.mark.asyncio
async def test_punctual_tool_availability_three_distinct_results():
    uc, ctx = real_service(status="INDETERMINADO")
    args = AvailabilityArguments(product_id=rows(await uc.search(SearchArguments(), ctx))[0]["product_id"])
    handlers = build_catalog_handlers(uc)
    valid = await uc.availability(args, ctx)
    uc._inventory.get_availability = AsyncMock(return_value=[])
    missing = await uc.availability(args, ctx)
    assert valid["texto"] != missing["texto"] and missing["missing_data"] == ["availability"]
    uc._inventory.get_availability = AsyncMock(side_effect=ModuleUnavailableError("productos"))
    with pytest.raises(CatalogError):
        await uc.availability(args, ctx)
    error = await handlers["consultar_disponibilidad"](args, ctx)
    assert error.code == "SERVICIO_NO_DISPONIBLE"
    assert error.data["texto"] not in {valid["texto"], missing["texto"]}


@pytest.mark.asyncio
async def test_recommendation_category_failure_retains_remaining_candidates():
    uc, ctx = service()
    original = uc._catalog.search_products
    uc._catalog.list_categories = AsyncMock(return_value=[
        {"category_id": "cat-zapatillas", "name": "Zapatillas"},
        {"category_id": "cat-medias", "name": "Medias"}])

    async def search(**kwargs):
        if kwargs.get("category_id") == "cat-medias":
            raise ModuleUnavailableError("productos")
        return await original(**kwargs)

    uc._catalog.search_products = search
    result = await uc.recommend(RecommendationArguments(actividad="running"), ctx)
    assert result["partial"] and rows(result)


@pytest.mark.asyncio
async def test_cross_sell_openapi_mock_contract_not_live_g6_evidence():
    """OpenAPI Recomendacion required fields; synthetic data, NOT live G6 evidence."""
    uc, ctx = service()
    body = {"recommendations": [
        {"product_id": "1", "type": "CROSS_SELL", "rule_priority": 0,
         "presentation_order": 1, "current_price": 81, "availability": "DISPONIBLE"},
        {"product_id": "2", "type": "UPSELL", "rule_priority": 0,
         "presentation_order": 2, "current_price": None, "availability": "DISPONIBLE"}]}
    async with httpx.AsyncClient(base_url="http://g6", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as client:
        uc._catalog.get_recommendations = ProductsClient("http://g6", client=client).get_recommendations
        result = await uc.cross_sell("0", ctx)
    assert [r["product_id"] for r in result[0]["datos"]["productos"]] == ["1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_cross_sell_empty_or_upstream_failure_is_silent(failed):
    uc, ctx = service()
    uc._catalog.get_recommendations = AsyncMock(side_effect=ModuleUnavailableError("productos")) if failed else AsyncMock(return_value=[])
    assert await uc.cross_sell("0", ctx) == []


@pytest.mark.asyncio
async def test_price_row_invalid_isolated_and_completed_calls_survive_stage_timeout(monkeypatch):
    uc, ctx = service()
    monkeypatch.setattr(module, "COMMERCIAL_BUDGET_SECONDS", 0.03)
    original = uc._catalog.get_prices

    async def prices(skus):
        if "SKU5" in skus:
            await asyncio.sleep(1)
        result = await original(skus)
        return [{**row, "precio_regular": "invalid"} if row["sku"] == "SKU0" else row for row in result]

    uc._catalog.get_prices = prices
    result = await uc.search(SearchArguments(), ctx)
    assert result["partial"]
    assert rows(result)[0]["missing_data"] == ["price"]
    assert rows(result)[1]["price"] is not None
    assert rows(result)[-1]["price"] is None


@pytest.mark.asyncio
async def test_inventory_failed_batch_preserves_other_batch(monkeypatch):
    uc, ctx = service()
    monkeypatch.setattr(module, "INVENTORY_BATCH_SIZE", 4)
    original = uc._inventory.get_availability

    async def availability(skus):
        if "SKU0" in skus:
            raise ModuleRequestError("productos", 422, "VALIDACION")
        return await original(skus)

    uc._inventory.get_availability = availability
    result = await uc.search(SearchArguments(), ctx)
    assert rows(result)[0]["missing_data"] == ["availability"]
    assert rows(result)[-1]["missing_data"] == []
    assert rows(result)[-1]["availability"] == "DISPONIBLE"


@pytest.mark.asyncio
async def test_offers_both_branches_timeout_is_total_error(monkeypatch):
    uc, ctx = service()
    monkeypatch.setattr(discovery, "OFFERS_BUDGET_SECONDS", 0.3)

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    uc._catalog.list_promotions = slow
    uc._catalog.search_products = slow
    with pytest.raises(CatalogError):
        await uc.offers(PromotionArguments(), ctx)


def test_rest_punctual_missing_and_obtained_indeterminate_are_distinct():
    uc, _ = real_service(status="INDETERMINADO")
    app = FastAPI()
    app.include_router(create_catalog_router(lambda: uc))
    with TestClient(app) as client:
        obtained = client.get("/catalogo/disponibilidad", params={"sku": SKU}).json()[0]
        uc._inventory.get_availability = AsyncMock(return_value=[])
        missing = client.get("/catalogo/disponibilidad", params={"sku": SKU}).json()[0]
    assert obtained["status"] == missing["status"] == "INDETERMINADO"
    assert obtained["missing_data"] == [] and missing["missing_data"] == ["availability"]
    assert obtained["mensaje"] != missing["mensaje"]


@pytest.mark.asyncio
async def test_bounded_offer_scan_without_failures_is_not_flagged_partial():
    """Stopping at the carousel size is a scan limit, not data that failed to load."""
    uc, ctx = service()
    original = uc._catalog.search_products

    async def search(**kwargs):
        return {**await original(**kwargs), "total": 40}

    uc._catalog.search_products = search
    result = await uc.search(SearchArguments(soloOfertas=True, tamanio=5), ctx, discover_offers=True)
    assert result["complete"] is False and result["total_scope"] == "scanned_pages"
    assert result["partial"] is False
    assert next(b for b in result["bloques"] if b["tipo"] == "CARRUSEL_PRODUCTOS")["datos"]["partial"] is False
    assert "no pudimos" not in result["texto"].casefold()
    assert "puede haber más ofertas" in result["texto"]


@pytest.mark.asyncio
async def test_offer_scan_stops_when_another_page_cannot_fit_the_budget(monkeypatch):
    """A slow validated page is answered at once instead of waiting to discard the next one."""
    uc, ctx = service()
    original = uc._catalog.search_products
    monkeypatch.setattr(module, "OFFER_DISCOVERY_SECONDS", 0.3)
    calls = []

    async def search(**kwargs):
        calls.append(kwargs.get("page", 1))
        await asyncio.sleep(0.2)
        return {**await original(**kwargs), "total": 40}

    uc._catalog.search_products = search
    result = await uc.search(SearchArguments(soloOfertas=True, tamanio=10), ctx, discover_offers=True)
    assert calls == [1] and result["pages_scanned"] == 1
    assert result["partial"] and result["complete"] is False
    assert rows(result) and "puede haber más ofertas" in result["texto"]
