"""Catalog use case flows (SPEC-06, SPEC-09, SPEC-10). Fakes only: no network, no database."""
import uuid

import pytest

from src.application.tools.catalog_tools import (
    AvailabilityArguments,
    ProductArguments,
    SearchArguments,
)
from src.application.tools.tool_registry import ExecutionContext
from src.application.use_cases.manage_catalog_use_case import (
    CatalogError,
    ManageCatalogUseCase,
)
from tests.fakes.fake_commerce import (
    FakeCatalogPort,
    FakeConversations,
    FakeInventoryPort,
    FakeUow,
)

CONVERSATION_ID = uuid.uuid4()
CUSTOMER_ID = uuid.uuid4()


def make_catalog(statuses=None, products=None, catalog=None, inventory=None):
    return ManageCatalogUseCase(
        catalog or FakeCatalogPort(products=products),
        inventory or FakeInventoryPort(statuses or {}),
        FakeConversations(),
        FakeUow(),
    )


def customer_context(conversation_id=CONVERSATION_ID):
    return ExecutionContext(
        conversacion_id=str(conversation_id), cliente_id=str(CUSTOMER_ID),
        chat_sid=None,
    )


def owned_catalog(catalog, conversation_id=CONVERSATION_ID):
    catalog._conversations.add_conversation(
        conversation_id, customer_id=CUSTOMER_ID, context={}
    )
    return catalog


@pytest.mark.asyncio
async def test_search_returns_carousel_and_saves_memory():
    catalog = owned_catalog(make_catalog())
    result = await catalog.search(
        SearchArguments(q="Nike"), customer_context()
    )
    assert result["code"] == "OK"
    assert result["bloques"][0]["tipo"] == "CARRUSEL_PRODUCTOS"
    memory = catalog._conversations.rows[CONVERSATION_ID].context
    assert memory["ultimoCarrusel"]
    assert memory["filtrosVigentes"]["q"] == "Nike"


@pytest.mark.asyncio
async def test_search_unknown_brand_asks_without_calling_modules():
    catalog = owned_catalog(make_catalog())
    result = await catalog.search(
        SearchArguments(marca="Inexistente"), customer_context()
    )
    assert "Inexistente" in result["texto"]
    assert result["bloques"] == []


@pytest.mark.asyncio
async def test_search_empty_results_hint():
    catalog = owned_catalog(make_catalog(products=[]))
    result = await catalog.search(SearchArguments(q="zzz"), customer_context())
    assert "No encontré" in result["texto"]


@pytest.mark.asyncio
async def test_search_unknown_conversation_raises_not_found():
    with pytest.raises(CatalogError) as error:
        await make_catalog().search(SearchArguments(q="x"), customer_context())
    assert error.value.code == "RECURSO_NO_ENCONTRADO"


@pytest.mark.asyncio
async def test_detail_by_carousel_ordinal():
    catalog = owned_catalog(make_catalog())
    await catalog.search(SearchArguments(q="Revolution"), customer_context())
    result = await catalog.detail(
        ProductArguments(productoRef=1), customer_context()
    )
    assert result["bloques"][0]["tipo"] == "DETALLE_PRODUCTO"
    assert "prod-001" in result["bloques"][0]["datos"]["producto"]["product_id"]


@pytest.mark.asyncio
async def test_detail_missing_product_maps_to_unavailable():
    catalog = owned_catalog(make_catalog())
    with pytest.raises(CatalogError) as error:
        await catalog.detail(
            ProductArguments(product_id="prod-missing"), customer_context()
        )
    assert error.value.code == "PRODUCTO_NO_DISPONIBLE"


@pytest.mark.asyncio
async def test_availability_in_stock_answers_text():
    catalog = owned_catalog(make_catalog(
        {"NIK-REV7-42-NEG": "DISPONIBLE"}
    ))
    result = await catalog.availability(
        AvailabilityArguments(product_id="prod-001", talla="42", color="negro"),
        customer_context(),
    )
    assert "stock" in result["texto"]


@pytest.mark.asyncio
async def test_availability_out_of_stock_suggests_similar():
    catalog = owned_catalog(make_catalog(
        {"NIK-REV7-42-NEG": "AGOTADO", "NIK-REV7-42-BLA": "AGOTADO",
         "NIK-REV7-43-NEG": "AGOTADO"}
    ))
    result = await catalog.availability(
        AvailabilityArguments(product_id="prod-001", talla="42", color="negro"),
        customer_context(),
    )
    assert "agotado" in result["texto"]


@pytest.mark.asyncio
async def test_availability_missing_size_asks_for_it():
    catalog = owned_catalog(make_catalog())

    result = await catalog.availability(
        AvailabilityArguments(product_id="prod-001"), customer_context()
    )
    assert result["bloques"][0]["tipo"] == "SELECTOR_VARIANTE"


class CountingCatalog(FakeCatalogPort):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.price_calls = 0

    async def get_prices(self, skus, **kwargs):
        self.price_calls += 1
        return await super().get_prices(skus, **kwargs)


class CountingInventory(FakeInventoryPort):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.availability_calls = 0

    async def get_availability(self, skus, **kwargs):
        self.availability_calls += 1
        return await super().get_availability(skus, **kwargs)


@pytest.mark.asyncio
async def test_search_batches_prices_and_availability_in_bulk():
    catalog_port = CountingCatalog()
    inventory_port = CountingInventory()
    catalog = owned_catalog(make_catalog(catalog=catalog_port, inventory=inventory_port))

    result = await catalog.search(SearchArguments(tamanio=10), customer_context())

    assert result["bloques"][0]["tipo"] == "CARRUSEL_PRODUCTOS"
    assert len(result["bloques"][0]["datos"]["productos"]) > 1
    assert catalog_port.price_calls == 1
    assert inventory_port.availability_calls == 1


@pytest.mark.asyncio
async def test_out_of_stock_suggests_similar_with_real_reference_price():
    products = [
        {"product_id": "prod-a", "name": "Runner A", "category_id": "cat-zapatillas",
         "sku_base": "A-BASE", "precio_regular": 100.0, "precio_oferta": 100.0},
        {"product_id": "prod-b", "name": "Runner B", "category_id": "cat-zapatillas",
         "sku_base": "B-BASE", "precio_regular": 105.0, "precio_oferta": 105.0},
    ]
    details = {
        "prod-a": {"product_id": "prod-a", "name": "Runner A",
                   "category_id": "cat-zapatillas", "tiene_variantes": True,
                   "variants": [{"sku": "A-BASE-42", "talla": "42", "color": "negro",
                                 "precio_regular": 100.0, "precio_oferta": 100.0}]},
    }
    catalog = owned_catalog(make_catalog(
        catalog=FakeCatalogPort(products=products, details=details),
        inventory=FakeInventoryPort({"A-BASE-42": "AGOTADO"}),
    ))

    result = await catalog.availability(
        AvailabilityArguments(product_id="prod-a", talla="42", color="negro"),
        customer_context(),
    )

    productos = result["bloques"][0]["datos"]["productos"]
    assert [p["product_id"] for p in productos] == ["prod-b"]


@pytest.mark.asyncio
async def test_option_cache_is_not_shared_between_use_cases():
    first = owned_catalog(make_catalog())
    await first.search(SearchArguments(q="Nike"), customer_context())

    assert first._option_cache.get("categories") is not None
    assert make_catalog()._option_cache.get("categories") is None
