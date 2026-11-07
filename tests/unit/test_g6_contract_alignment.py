"""Published G6 a7778ec schemas; isolated HTTP evidence, never live homologation."""
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.adapters.inbound.http.catalog_router import create_catalog_router
from src.adapters.inbound.http.problem import register_problem_handlers
from src.adapters.outbound.fakes.fake_products import FakeCatalogPort
from src.adapters.outbound.http_clients.products_client import ProductsClient
from src.application.tools.catalog_tools import AvailabilityArguments, SearchArguments
from src.application.use_cases.manage_catalog_use_case import CatalogError
from tests.unit.test_astra_regressions import cards, g6_detail, g6_service

BASE = "http://g6.test"


@pytest.mark.parametrize("path,envelope,method,row", [
    # OpenAPI GET /categorias and /marcas 200 schemas + Categoria / Marca.
    ("categorias", "categories", "list_categories",
     {"categoria_id": "cat", "name": "Zapatillas", "status": "ACTIVA", "parent_id": None}),
    ("marcas", "brands", "list_brands",
     {"marca_id": "brand", "name": "Nike", "status": "ACTIVA", "description": None}),
])
@pytest.mark.parametrize("shape", ["published", "items", "array", "empty"])
@respx.mock
@pytest.mark.asyncio
async def test_taxonomy_published_and_compatible_envelopes(path, envelope, method, row, shape):
    payload = {envelope: [row]} if shape == "published" else (
        {"items": [row]} if shape == "items" else [row] if shape == "array" else {envelope: []}
    )
    respx.get(f"{BASE}/{path}").respond(200, json=payload)
    result = await getattr(ProductsClient(BASE), method)()
    assert result == ([] if shape == "empty" else [row])


@pytest.mark.asyncio
async def test_search_filters_size_and_color_locally_before_commercial_reads():
    detail = g6_detail("p", "brand", ("42", "43"))
    for variant in detail["variants"]:
        variant["identifying_attributes"].append({"nombre": "Color", "valor": "Negro"})
    uc, ctx = g6_service([detail])
    result = await uc.search(SearchArguments(talla="42", color="negro"), ctx)
    row = cards(result)[0]
    assert [v["sku"] for v in row["variants"]] == ["p-42"]
    assert row["single_sku"] == "p-42"
    assert result["total_scope"] == "page" and result["total"] == 1
    uc._catalog.get_prices.assert_awaited_once_with(["p-42"])
    uc._inventory.get_availability.assert_awaited_once_with(["p-42"])
    sent = uc._catalog.search_products.call_args.kwargs
    assert "talla" not in sent and "color" not in sent


@pytest.mark.asyncio
async def test_optional_identifying_attributes_never_invent_a_size():
    detail = g6_detail("p", "brand")
    del detail["variants"][0]["identifying_attributes"]
    uc, ctx = g6_service([detail])
    assert cards(await uc.search(SearchArguments(talla="42"), ctx)) == []
    uc._catalog.get_prices.assert_not_awaited()
    uc._inventory.get_availability.assert_not_awaited()
    row = cards(await uc.search(SearchArguments(), ctx))[0]
    assert row["variants"][0]["talla"] is None


@pytest.mark.asyncio
async def test_missing_separate_price_does_not_use_embedded_catalog_price():
    detail = g6_detail("p", "brand")
    detail["precio_regular"] = 1
    detail["variants"][0]["precio_oferta"] = 1
    uc, ctx = g6_service([detail])
    uc._catalog.get_prices.return_value = []
    row = cards(await uc.search(SearchArguments(), ctx))[0]
    assert row["price"] is None and row["price_text"] == "Precio no disponible"
    assert row["availability"] == "DISPONIBLE"


@pytest.mark.asyncio
async def test_commercial_queries_are_bounded_and_batched():
    detail = g6_detail("p", "brand", tuple(str(i) for i in range(125)))
    uc, ctx = g6_service([detail])
    row = cards(await uc.search(SearchArguments(), ctx))[0]
    assert len(row["variants"]) == 100
    assert len(uc._catalog.get_prices.await_args_list) == 20
    assert all(len(call.args[0]) <= 5 for call in uc._catalog.get_prices.await_args_list)
    stock_batches = [call.args[0] for call in uc._inventory.get_availability.await_args_list]
    assert len(stock_batches) == 5 and all(len(batch) == 20 for batch in stock_batches)
    assert len({sku for batch in stock_batches for sku in batch}) == 100


@pytest.mark.asyncio
async def test_unknown_availability_does_not_claim_low_stock():
    uc, ctx = g6_service([g6_detail("p", "brand")])
    uc._inventory.get_availability.return_value = [{"sku": "p-42", "status": "INDETERMINADO"}]
    result = await uc.availability(AvailabilityArguments(product_id="p", talla="42"), ctx)
    assert result["texto"] == "No pudimos confirmar el stock."


@pytest.mark.parametrize("rows", [[], [{"sku": "other", "status": "DISPONIBLE"}],
                                  [{"sku": "p-42", "status": "UNKNOWN"}]])
@pytest.mark.asyncio
async def test_missing_or_incompatible_stock_is_an_error(rows):
    uc, ctx = g6_service([g6_detail("p", "brand")])
    uc._inventory.get_availability.return_value = rows
    with pytest.raises(CatalogError, match="ValueError"):
        await uc.availability(AvailabilityArguments(product_id="p", talla="42"), ctx)


@pytest.mark.parametrize("upstream,code,expected", [
    (401, "TOKEN_INVALIDO", 503), (403, "SCOPE_INSUFICIENTE", 503),
    (404, "RECURSO_NO_ENCONTRADO", 404), (429, "RATE_LIMIT_EXCEDIDO", 429),
    (500, "ERROR_INTERNO", 503), (503, "SERVICIO_NO_DISPONIBLE", 503),
])
def test_published_errors_do_not_expire_customer_sessions(upstream, code, expected):
    transport = httpx.MockTransport(lambda req: httpx.Response(
        upstream, json={"code": code}, headers={"Retry-After": "30"},
    ))
    uc, _ = g6_service([])
    uc._catalog = ProductsClient(BASE, client=httpx.AsyncClient(transport=transport, base_url=BASE))
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(create_catalog_router(lambda: uc), prefix="/api/v1")
    response = TestClient(app).get("/api/v1/catalogo/productos")
    assert response.status_code == expected
    assert response.json()["code"] == ("SERVICIO_NO_DISPONIBLE" if expected == 503 else code)
    if upstream in (429, 500, 503):
        assert response.headers["Retry-After"] == "30"


@pytest.mark.asyncio
async def test_seed_fakes_expose_catalog_without_embedded_prices():
    catalog = FakeCatalogPort()
    page = await catalog.search_products()
    assert page["items"] and all("categoria_id" in row for row in page["items"])
    detail = await catalog.get_product_detail("prod-001")
    assert all("variant_id" in row for row in detail["variants"])
    assert all("precio_regular" not in row and "stock" not in row
               for row in [detail, *detail["variants"], *page["items"]])
    price = await catalog.get_price(detail["variants"][0]["sku"])
    assert price["precio_regular"] == 329.9


def test_rest_size_filters_reach_g3_use_case():
    uc, _ = g6_service([])
    uc.search = AsyncMock(return_value={"bloques": [], "total": 0, "total_scope": "page"})
    app = FastAPI()
    app.include_router(create_catalog_router(lambda: uc), prefix="/api/v1")
    response = TestClient(app).get("/api/v1/catalogo/productos?talla=42&color=negro")
    assert response.status_code == 200
    args = uc.search.call_args.args[0]
    assert args.talla == "42" and args.color == "negro"
