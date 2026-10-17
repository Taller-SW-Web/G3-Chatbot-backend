"""Productos mocks: typed clients over BaseModuleClient + respx seeds.

Covers SPEC-06 (search), SPEC-09 (detail), SPEC-10 (availability) and the
SPEC-11 pricing hooks. No network, no real module, no database.
"""
import httpx
import pytest
import respx
from decimal import Decimal

from src.adapters.outbound.http_clients.base_client import ModuleUnavailableError
from src.adapters.outbound.http_clients.inventory_client import (
    INVENTORY_TIMEOUT_SECONDS,
    InventoryClient,
)
from src.adapters.outbound.http_clients.products_client import ProductsClient
from src.infrastructure.config.container import (
    provide_inventory_client,
    provide_products_client,
)
from src.infrastructure.config.settings import Settings
from src.ports.outbound.module_errors import ModuleRequestError
from tests.helpers import product_mocks

BASE_URL = "http://mock:4011"


def make_catalog() -> ProductsClient:
    return ProductsClient(base_url=BASE_URL, timeout_seconds=4.0)


def make_inventory() -> InventoryClient:
    return InventoryClient(base_url=BASE_URL)


@respx.mock
@pytest.mark.asyncio
async def test_search_sends_real_param_names_and_pagination():
    # The mock paginates the full seed catalog without applying filters:
    # this test pins the request shape, not the filtering itself.
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
    assert params["precioMin"] == "100.00"
    assert params["precioMax"] == "350.00"
    assert request.headers["x-correlation-id"] == "corr-1"


@respx.mock
@pytest.mark.asyncio
async def test_search_formats_float_prices_with_two_decimals():
    route = product_mocks.mock_product_search(BASE_URL)
    await make_catalog().search_products(min_price=0.1 + 0.2, max_price=Decimal("10.5"))
    params = dict(route.calls[0].request.url.params)
    assert params["precioMin"] == "0.30"
    assert params["precioMax"] == "10.50"


@pytest.mark.asyncio
async def test_search_rejects_negative_and_inverted_prices():
    catalog = make_catalog()
    with pytest.raises(ValueError):
        await catalog.search_products(min_price=-5)
    with pytest.raises(ValueError):
        await catalog.search_products(max_price=-5)
    with pytest.raises(ValueError):
        await catalog.search_products(min_price=400, max_price=100)


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
async def test_detail_not_found_raises_with_contract_code():
    product_mocks.mock_product_detail_not_found("prod-missing", BASE_URL)
    with pytest.raises(ModuleRequestError) as error:
        await make_catalog().get_product_detail("prod-missing")
    assert error.value.status == 404
    assert error.value.code == "RECURSO_NO_ENCONTRADO"


@respx.mock
@pytest.mark.asyncio
async def test_detail_encodes_path_identifiers():
    route = product_mocks.mock_product_detail("prod-001", BASE_URL)
    respx.get(f"{BASE_URL}/productos/a%2Fb").mock(
        return_value=httpx.Response(200, json={"product_id": "a/b"})
    )
    detail = await make_catalog().get_product_detail("a/b")
    assert detail["product_id"] == "a/b"
    assert not route.called


@respx.mock
@pytest.mark.asyncio
async def test_search_400_raises_with_validation_code():
    respx.get(f"{BASE_URL}/productos").mock(
        return_value=httpx.Response(400, json={"code": "VALIDACION"})
    )
    with pytest.raises(ModuleRequestError) as error:
        await make_catalog().search_products(q="x")
    assert error.value.status == 400
    assert error.value.code == "VALIDACION"


@respx.mock
@pytest.mark.asyncio
async def test_categories_401_raises_instead_of_shape_error():
    respx.get(f"{BASE_URL}/categorias").mock(
        return_value=httpx.Response(401, json={"code": "TOKEN_INVALIDO"})
    )
    with pytest.raises(ModuleRequestError) as error:
        await make_catalog().list_categories()
    assert (error.value.status, error.value.code) == (401, "TOKEN_INVALIDO")


@respx.mock
@pytest.mark.asyncio
async def test_4xx_reads_legacy_codigo_field():
    respx.get(f"{BASE_URL}/precios/skus/X").mock(
        return_value=httpx.Response(404, json={"codigo": "RECURSO_NO_ENCONTRADO"})
    )
    with pytest.raises(ModuleRequestError) as error:
        await make_catalog().get_price("X")
    assert (error.value.status, error.value.code) == (404, "RECURSO_NO_ENCONTRADO")


@respx.mock
@pytest.mark.asyncio
async def test_invalid_json_body_maps_to_unavailable():
    respx.get(f"{BASE_URL}/marcas").mock(
        return_value=httpx.Response(200, text="<html>nope</html>")
    )
    with pytest.raises(ModuleUnavailableError):
        await make_catalog().list_brands()


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
    assert dict(route_bulk.calls[0].request.url.params)["canal"] == "CHATBOT"


@respx.mock
@pytest.mark.asyncio
async def test_bulk_prices_reject_empty_and_over_20_skus():
    with pytest.raises(ValueError):
        await make_catalog().get_prices([])
    with pytest.raises(ValueError):
        await make_catalog().get_prices([f"SKU-{n}" for n in range(21)])


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
    params = dict(route.calls[0].request.url.params)
    assert params["skus"] == (
        "NIK-REV7-42-NEG,NIK-REV7-42-BLA,ADI-PRED-40-BLA"
    )
    assert params["canal"] == "CHATBOT"
    by_sku = {row["sku"]: row for row in rows}
    assert by_sku["NIK-REV7-42-NEG"]["status"] == "DISPONIBLE"
    assert by_sku["NIK-REV7-42-BLA"]["status"] == "STOCK_BAJO"
    assert by_sku["ADI-PRED-40-BLA"]["status"] == "AGOTADO"


@pytest.mark.asyncio
async def test_availability_rejects_empty_and_over_100_skus():
    inventory = make_inventory()
    with pytest.raises(ValueError):
        await inventory.get_availability([])
    with pytest.raises(ValueError):
        await inventory.get_availability([f"SKU-{n}" for n in range(101)])
    with pytest.raises(ValueError):
        await inventory.get_availability(["  "])
    with pytest.raises(ValueError):
        await inventory.get_availability(["A,B"])


@respx.mock
@pytest.mark.asyncio
async def test_availability_dedupes_before_the_limit():
    route = product_mocks.mock_availability(BASE_URL)
    rows = await make_inventory().get_availability(["NIK-REV7-42-NEG"] * 20)
    assert len(route.calls) == 1
    assert dict(route.calls[0].request.url.params)["skus"] == "NIK-REV7-42-NEG"
    assert rows[0]["sku"] == "NIK-REV7-42-NEG"


@respx.mock
@pytest.mark.asyncio
async def test_availability_409_raises_with_contract_code():
    respx.get(f"{BASE_URL}/inventario/disponibilidad/comercial").mock(
        return_value=httpx.Response(409, json={"code": "PRODUCTO_NO_DISPONIBLE"})
    )
    with pytest.raises(ModuleRequestError) as error:
        await make_inventory().get_availability(["SKU-X"])
    assert (error.value.status, error.value.code) == (409, "PRODUCTO_NO_DISPONIBLE")


@respx.mock
@pytest.mark.asyncio
async def test_availability_rejects_malformed_rows():
    respx.get(f"{BASE_URL}/inventario/disponibilidad/comercial").mock(
        return_value=httpx.Response(200, json=[{"sku": "  ", "status": "DISPONIBLE"}])
    )
    with pytest.raises(ValueError):
        await make_inventory().get_availability(["SKU-X"])


@respx.mock
@pytest.mark.asyncio
async def test_verify_quantities_posts_real_keys():
    route = product_mocks.mock_availability_verify(BASE_URL)
    import json

    verdicts = await make_inventory().verify_quantities(
        [{"sku": "NIK-REV7-42-NEG", "quantity": 2}]
    )
    by_sku = {verdict["sku"]: verdict for verdict in verdicts}
    assert by_sku["NIK-REV7-42-NEG"]["veredicto"] == "SUFICIENTE"
    assert by_sku["NIK-REV7-42-BLA"]["veredicto"] == "INSUFICIENTE"
    assert by_sku["AND-POLO-M-NEG"]["veredicto"] == "INDETERMINADO"
    body = json.loads(route.calls[0].request.content)
    assert body["channel_id"] == "CHATBOT"
    assert body["lines"] == [{"sku": "NIK-REV7-42-NEG", "quantity": 2}]


@pytest.mark.asyncio
async def test_verify_quantities_rejects_bad_lines():
    inventory = make_inventory()
    with pytest.raises(ValueError):
        await inventory.verify_quantities([])
    with pytest.raises(ValueError):
        await inventory.verify_quantities([{"sku": "A", "quantity": 0}])
    with pytest.raises(ValueError):
        await inventory.verify_quantities([{"sku": "A,B", "quantity": 1}])
    with pytest.raises(ValueError):
        await inventory.verify_quantities([{"sku": "A"}])


@respx.mock
@pytest.mark.asyncio
async def test_sku_availability_found_and_missing():
    product_mocks.mock_availability(BASE_URL)
    inventory = make_inventory()
    row = await inventory.get_sku_availability("NIK-REV7-42-NEG")
    assert row is not None and row["status"] == "DISPONIBLE"
    assert await inventory.get_sku_availability("SKU-UNKNOWN") is None


@respx.mock
@pytest.mark.asyncio
async def test_promotions_list_resolves_names():
    product_mocks.mock_promotions_list(BASE_URL)
    promotions = await make_catalog().list_promotions()
    assert promotions[0] == {
        "promotion_id": "promo-10", "name": "Cyber 10%",
        "channel_id": "CHATBOT",
    }


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


# --- Residual fixes after the products client review ---------------------------------


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "RECURSO_NO_ENCONTRADO"),
        (400, "VALIDACION"),
        (401, "NO_AUTORIZADO"),
        (409, "HTTP_409"),
    ],
)
async def test_non_json_4xx_keeps_status_and_falls_back_to_a_code(status, code):
    respx.get(f"{BASE_URL}/productos/prod-1").mock(
        return_value=httpx.Response(status, text="<html>error</html>")
    )
    with pytest.raises(ModuleRequestError) as error:
        await make_catalog().get_product_detail("prod-1")
    assert (error.value.status, error.value.code) == (status, code)


@respx.mock
@pytest.mark.asyncio
async def test_non_json_4xx_on_inventory_keeps_status():
    respx.get(f"{BASE_URL}/inventario/disponibilidad/comercial").mock(
        return_value=httpx.Response(404, text="<html>not found</html>")
    )
    with pytest.raises(ModuleRequestError) as error:
        await make_inventory().get_availability(["SKU-1"])
    assert error.value.status == 404


@respx.mock
@pytest.mark.asyncio
async def test_non_json_2xx_is_module_unavailable():
    respx.get(f"{BASE_URL}/productos/prod-1").mock(
        return_value=httpx.Response(200, text="<html>oops</html>")
    )
    with pytest.raises(ModuleUnavailableError):
        await make_catalog().get_product_detail("prod-1")


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"code": "X"}, "<html>moved</html>"])
async def test_3xx_is_module_unavailable_not_a_request_error(body):
    kwargs = {"json": body} if isinstance(body, dict) else {"text": body}
    respx.get(f"{BASE_URL}/productos/prod-1").mock(
        return_value=httpx.Response(302, **kwargs)
    )
    with pytest.raises(ModuleUnavailableError):
        await make_catalog().get_product_detail("prod-1")


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadError("connection reset"),
        httpx.RemoteProtocolError("server disconnected"),
        httpx.WriteError("broken pipe"),
    ],
)
async def test_transport_errors_become_module_unavailable(exc):
    respx.get(f"{BASE_URL}/productos/prod-1").mock(side_effect=exc)
    with pytest.raises(ModuleUnavailableError) as error:
        await make_catalog().get_product_detail("prod-1")
    assert error.value.module == "productos"


def test_sku_caps_are_defined_once():
    from src.adapters.outbound.http_clients import products_client
    from src.ports.outbound import product_repository_port

    assert (
        products_client.MAX_SKUS_PER_CALL
        is product_repository_port.MAX_SKUS_PER_CALL
    )
    assert product_repository_port.MAX_SKUS_PER_CALL == 20
    assert product_repository_port.MAX_COMERCIAL_SKUS == 100
