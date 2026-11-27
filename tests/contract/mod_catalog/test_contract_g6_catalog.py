"""G6 Productos contract: search and product detail (OpenAPI 0.7.0-rc.3)."""
import httpx
import pytest
import respx

from src.modules.mod_catalog.adapters.products_client import ProductsClient
from src.shared.domain.module_errors import ModuleRequestError

BASE_URL = "http://mock-productos:4011/api/v1"


class StubServiceTokens:
    def __init__(self) -> None:
        self.requests: list[dict[str, str | None]] = []

    async def get_token(self, *, correlation_id=None, scope=None, audience=None) -> str:
        self.requests.append({"scope": scope, "audience": audience})
        return "svc-token"


def make_client(tokens=None) -> ProductsClient:
    return ProductsClient(
        base_url=BASE_URL,
        timeout_seconds=4.0,
        service_token_provider=tokens or StubServiceTokens(),
    )


@respx.mock
@pytest.mark.asyncio
async def test_search_products_sends_documented_filters_and_never_estado(load_fixture):
    body = load_fixture("g6_productos_page.json")
    route = respx.get(f"{BASE_URL}/productos").mock(
        return_value=httpx.Response(200, json=body)
    )
    tokens = StubServiceTokens()

    result = await make_client(tokens).search_products(
        q="audífonos",
        category_id="CAT-AUDIO",
        brand_id="MARCA-SONY",
        page=2,
        size=5,
        min_price=50,
        max_price=150,
    )

    request = route.calls[0].request
    params = dict(request.url.params)
    assert request.headers["authorization"] == "Bearer svc-token"
    assert params == {
        "canal": "CHATBOT",
        "pagina": "2",
        "tamanio": "5",
        "q": "audífonos",
        "categoriaId": "CAT-AUDIO",
        "marcaId": "MARCA-SONY",
        "precioMin": "50.00",
        "precioMax": "150.00",
    }
    assert "estado" not in params
    assert result == body
    assert tokens.requests == [
        {"scope": "catalogo:leer", "audience": "api-productos"}
    ]


@respx.mock
@pytest.mark.asyncio
async def test_product_detail_reads_documented_variants_and_brand(load_fixture):
    body = load_fixture("g6_producto_detalle.json")
    route = respx.get(f"{BASE_URL}/productos/PROD-101").mock(
        return_value=httpx.Response(200, json=body)
    )

    result = await make_client().get_product_detail("PROD-101")

    assert dict(route.calls[0].request.url.params) == {"canal": "CHATBOT"}
    assert result["product_id"] == "PROD-101"
    assert result["brand_id"] == "MARCA-SONY"
    assert result["brand_name"] == "Sony"
    assert result["variants"][0]["sku"] == "SKU-AUR-01-NEG"
    assert result["variants"][0]["talla"] == "Única"
    assert result["variants"][0]["color"] == "Negro"


@respx.mock
@pytest.mark.asyncio
async def test_product_404_maps_the_documented_code(load_fixture):
    body = load_fixture("g6_error_producto_no_encontrado.json")
    respx.get(f"{BASE_URL}/productos/PROD-404").mock(
        return_value=httpx.Response(404, json=body)
    )

    with pytest.raises(ModuleRequestError) as exc:
        await make_client().get_product_detail("PROD-404")

    assert exc.value.status == 404
    assert exc.value.code == "PRODUCTO_NO_ENCONTRADO"
