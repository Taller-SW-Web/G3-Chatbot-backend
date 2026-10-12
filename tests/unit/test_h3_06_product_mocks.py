"""H3-06 Productos mocks: typed clients over BaseModuleClient + respx seeds.

Covers SPEC-06 (search), SPEC-09 (detail), SPEC-10 (availability) and the
SPEC-11 pricing hooks. No network, no real module, no database.
"""
import httpx
import pytest
import respx

from src.adapters.outbound.http_clients.base_client import ModuleUnavailableError
from src.adapters.outbound.http_clients.inventario_api_adapter import (
    INVENTORY_TIMEOUT_SECONDS,
    InventoryClient,
)
from src.adapters.outbound.http_clients.productos_api_adapter import ProductsClient
from src.infrastructure.config.container import (
    provide_inventory_client,
    provide_products_client,
)
from src.infrastructure.config.settings import Settings
from tests.helpers import product_mocks

BASE_URL = "http://mock:4011"


def make_catalog() -> ProductsClient:
    return ProductsClient(base_url=BASE_URL, timeout_seconds=4.0)


def make_inventory() -> InventoryClient:
    return InventoryClient(base_url=BASE_URL)


@respx.mock
@pytest.mark.asyncio
async def test_search_sends_real_param_names_and_pagination():
    route = product_mocks.mock_product_search(BASE_URL)
    result = await make_catalog().search_products(
        q="zapatillas",
        category_id="cat-zapatillas",
        brand_id="brand-nike",
        min_price=100,
        max_price=350,
        page=1,
        size=10,
        correlation_id="corr-1",
    )
    assert result["total"] == 100
    assert len(result["items"]) == 10
    assert result["items"][0]["product_id"] == "prod-001"
    request = route.calls[0].request
    params = dict(request.url.params)
    assert params["q"] == "zapatillas"
    assert params["categoriaId"] == "cat-zapatillas"
    assert params["marcaId"] == "brand-nike"
    assert params["canal"] == "CHATBOT"
    assert params["pagina"] == "1"
    assert params["tamanio"] == "10"
    assert params["estado"] == "ACTIVO"
    assert params["precioMin"] == "100"
    assert params["precioMax"] == "350"
    assert request.headers["x-correlation-id"] == "corr-1"


@pytest.mark.asyncio
async def test_search_rejects_bad_pagination():
    catalog = make_catalog()
    with pytest.raises(ValueError):
        await catalog.search_products(page=0)
    with pytest.raises(ValueError):
        await catalog.search_products(size=11)
    with pytest.raises(ValueError):
        await catalog.search_products(size=0)


@respx.mock
@pytest.mark.asyncio
async def test_search_paginates_full_catalog():
    product_mocks.mock_product_search(BASE_URL)
    catalog = make_catalog()
    page2 = await catalog.search_products(page=2, size=10)
    assert page2["total"] == 100
    assert page2["pagina"] == 2
    assert [item["product_id"] for item in page2["items"]][0] == "prod-011"
    last = await catalog.search_products(page=10, size=10)
    assert [item["product_id"] for item in last["items"]][-1] == "prod-100"
    short = await catalog.search_products(page=1, size=5)
    assert len(short["items"]) == 5


@respx.mock
@pytest.mark.asyncio
async def test_detail_returns_variants_with_real_keys():
    product_mocks.mock_product_detail("prod-001", BASE_URL)
    detail = await make_catalog().get_product_detail("prod-001")
    assert detail["product_id"] == "prod-001"
    assert detail["tiene_variantes"] is True
    assert len(detail["variants"]) == 3
    assert detail["variants"][0]["sku"] == "NIK-REV7-42-NEG"


@respx.mock
@pytest.mark.asyncio
async def test_detail_not_found_carries_product_unavailable_code():
    product_mocks.mock_product_detail_not_found("prod-missing", BASE_URL)
    client = make_catalog()
    response = await client._request("GET", "/productos/prod-missing")
    assert response.status_code == 404
    assert response.json()["code"] == "PRODUCT_UNAVAILABLE"


@respx.mock
@pytest.mark.asyncio
async def test_categories_and_brands():
    product_mocks.mock_categories(BASE_URL)
    product_mocks.mock_brands(BASE_URL)
    catalog = make_catalog()
    categories = await catalog.list_categories()
    brands = await catalog.list_brands()
    assert [c["category_id"] for c in categories] == [
        "cat-zapatillas",
        "cat-polos",
        "cat-mochilas",
    ]
    assert [b["brand_id"] for b in brands] == [
        "brand-nike",
        "brand-adidas",
        "brand-andes",
    ]


@respx.mock
@pytest.mark.asyncio
async def test_single_and_bulk_prices():
    product_mocks.mock_price("NIK-REV7-42-NEG", BASE_URL)
    route_bulk = product_mocks.mock_prices_bulk(BASE_URL)
    catalog = make_catalog()
    price = await catalog.get_price("NIK-REV7-42-NEG")
    assert price["sku"] == "NIK-REV7-42-NEG"
    assert price["precio_oferta"] == 279.9
    assert price["currency"] == "PEN"
    prices = await catalog.get_prices(["NIK-REV7-42-NEG", "NIK-POLO-UNICO"])
    assert len(prices) == 2
    assert dict(route_bulk.calls[0].request.url.params)["channel_id"] == "CHATBOT"


@respx.mock
@pytest.mark.asyncio
async def test_bulk_prices_reject_empty_skus():
    with pytest.raises(ValueError):
        await make_catalog().get_prices([])


@respx.mock
@pytest.mark.asyncio
async def test_evaluate_promotions_posts_real_keys():
    route = product_mocks.mock_promotions_evaluate(BASE_URL)
    import json

    result = await make_catalog().evaluate_promotions(
        [{"sku": "NIK-REV7-42-NEG", "quantity": 1}],
        coupon_code="SPORT10",
        customer_ref="cust-123",
    )
    assert result["total"] == 251.9
    assert result["coupon_code"] == "SPORT10"
    body = json.loads(route.calls[0].request.content)
    assert body["channel_id"] == "CHATBOT"
    assert body["coupon_code"] == "SPORT10"
    assert body["customer_ref"] == "cust-123"
    assert body["lines"] == [{"sku": "NIK-REV7-42-NEG", "quantity": 1}]
    assert "at" in body


@respx.mock
@pytest.mark.asyncio
async def test_evaluate_normalizes_codigo_to_code():
    respx.post(f"{BASE_URL}/promociones/evaluar").mock(
        return_value=httpx.Response(200, json={"codigo": "CUPON_INVALIDO"})
    )
    result = await make_catalog().evaluate_promotions([{"sku": "X", "quantity": 1}])
    assert result["code"] == "CUPON_INVALIDO"


@respx.mock
@pytest.mark.asyncio
async def test_availability_single_bulk_call_with_live_rows():
    route = product_mocks.mock_availability(BASE_URL)
    rows = await make_inventory().get_availability(
        ["NIK-REV7-42-NEG", "NIK-REV7-42-BLA", "ADI-PRED-40-BLA"]
    )
    assert len(route.calls) == 1
    assert dict(route.calls[0].request.url.params)["skus"] == (
        "NIK-REV7-42-NEG,NIK-REV7-42-BLA,ADI-PRED-40-BLA"
    )
    by_sku = {row["sku"]: row for row in rows}
    assert by_sku["NIK-REV7-42-NEG"]["available"] == 12
    assert by_sku["NIK-REV7-42-NEG"]["status"] == "IN_STOCK"
    assert by_sku["NIK-REV7-42-BLA"]["status"] == "LOW_STOCK"
    assert by_sku["ADI-PRED-40-BLA"]["available"] == 0


@pytest.mark.asyncio
async def test_availability_rejects_empty_and_over_20_skus():
    inventory = make_inventory()
    with pytest.raises(ValueError):
        await inventory.get_availability([])
    with pytest.raises(ValueError):
        await inventory.get_availability([f"SKU-{n}" for n in range(21)])


@respx.mock
@pytest.mark.asyncio
async def test_sku_availability_found_and_missing():
    product_mocks.mock_availability(BASE_URL)
    inventory = make_inventory()
    row = await inventory.get_sku_availability("NIK-REV7-42-NEG")
    assert row is not None and row["available"] == 12
    assert await inventory.get_sku_availability("SKU-UNKNOWN") is None


@respx.mock
@pytest.mark.asyncio
async def test_module_down_maps_to_unavailable():
    product_mocks.mock_module_down("GET", "/productos", BASE_URL)
    with pytest.raises(ModuleUnavailableError):
        await make_catalog().search_products(q="zapatillas")


@respx.mock
@pytest.mark.asyncio
async def test_module_5xx_maps_to_unavailable():
    respx.get(f"{BASE_URL}/productos").mock(
        return_value=httpx.Response(503, json={"error": "down"})
    )
    with pytest.raises(ModuleUnavailableError):
        await make_catalog().search_products(q="zapatillas")


def test_container_wiring_uses_settings():
    settings = Settings(products_base_url="http://mock:4011")
    catalog = provide_products_client(settings)
    inventory = provide_inventory_client(settings)
    assert catalog.base_url == "http://mock:4011"
    assert catalog.timeout_seconds == 4.0
    assert inventory.base_url == "http://mock:4011"
    assert inventory.timeout_seconds == INVENTORY_TIMEOUT_SECONDS == 3.0
