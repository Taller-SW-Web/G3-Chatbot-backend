"""Commercial wiring: fake/http selection, full registry, settings (ADR-0022).

No network, no database.
"""
import pytest

from src.adapters.outbound.fakes.fake_products import (
    FakeCatalogPort,
    FakeInventoryPort,
)
from src.infrastructure.config.container import (
    provide_product_catalog,
    provide_product_inventory,
)
from src.infrastructure.config.settings import Settings, get_settings


def test_fake_mode_returns_in_memory_ports():
    settings = Settings(modules_mode="fake")
    assert isinstance(provide_product_catalog(settings), FakeCatalogPort)
    assert isinstance(provide_product_inventory(settings), FakeInventoryPort)


def test_http_mode_returns_typed_clients():
    from src.adapters.outbound.http_clients.inventory_client import InventoryClient
    from src.adapters.outbound.http_clients.products_client import ProductsClient

    settings = Settings(modules_mode="http")
    assert isinstance(provide_product_catalog(settings), ProductsClient)
    assert isinstance(provide_product_inventory(settings), InventoryClient)


def test_modules_mode_rejects_unknown_values(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("MODULES_MODE", "prism")
    with pytest.raises(ValueError):
        get_settings()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_fake_catalog_answers_search_and_detail():
    catalog = FakeCatalogPort()
    page = await catalog.search_products(q="Nike")
    assert page["total"] > 0
    detail = await catalog.get_product_detail("prod-001")
    assert len(detail["variants"]) == 3
    price = await catalog.get_price("NIK-REV7-42-NEG")
    assert price["precio_oferta"] == 279.9


@pytest.mark.asyncio
async def test_fake_inventory_status_and_verdicts():
    inventory = FakeInventoryPort({"A": "AGOTADO"})
    rows = await inventory.get_availability(["A", "B"])
    assert [row["status"] for row in rows] == ["AGOTADO", "DISPONIBLE"]
    verdicts = await inventory.verify_quantities([{"sku": "A", "quantity": 1}])
    assert verdicts[0]["veredicto"] == "INSUFICIENTE"
