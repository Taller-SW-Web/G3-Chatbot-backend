"""Commercial wiring: fake/http selection, full registry, settings (ADR-0022).

No network, no database.
"""
from unittest.mock import MagicMock

import pytest

from src.modules.mod_checkout.module import ORDER_CONFIRMATION, provide_checkout_service
from src.modules.mod_catalog.adapters.fake_products import (
    FakeCatalogPort,
    FakeInventoryPort,
)
from src.modules.mod_catalog.module import (
    provide_product_catalog,
    provide_product_inventory,
)
from src.shared.infrastructure.config.settings import Settings, get_settings


def test_fake_mode_returns_in_memory_ports():
    settings = Settings(modules_mode="fake")
    assert isinstance(provide_product_catalog(settings), FakeCatalogPort)
    assert isinstance(provide_product_inventory(settings), FakeInventoryPort)


def test_http_mode_returns_typed_clients():
    from src.modules.mod_catalog.adapters.inventory_client import InventoryClient
    from src.modules.mod_catalog.adapters.products_client import ProductsClient

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
    inventory = FakeInventoryPort({"A": "AGOTADO"}, default="DISPONIBLE")
    rows = await inventory.get_availability(["A", "B"])
    assert [row["status"] for row in rows] == ["AGOTADO", "DISPONIBLE"]
    verdicts = await inventory.verify_quantities([{"sku": "A", "quantity": 1}])
    assert verdicts[0]["result"] == "INSUFICIENTE"
    assert verdicts[0]["requested_quantity"] == 1


@pytest.mark.asyncio
async def test_fake_inventory_does_not_invent_availability():
    """A SKU outside the seeds has no row: absence, never a default status."""
    inventory = FakeInventoryPort()
    assert await inventory.get_availability(["NOT-IN-SEEDS"]) == []
    assert await inventory.verify_quantities([{"sku": "NOT-IN-SEEDS", "quantity": 1}]) == []
    known = await inventory.get_availability(["NIK-REV7-42-NEG"])
    assert known == [{"sku": "NIK-REV7-42-NEG", "status": "DISPONIBLE"}]


def test_checkout_provider_wires_real_notification_service(monkeypatch):
    from src.shared.infrastructure.config import settings as settings_module
    from src.shared.infrastructure.di.registry import registry

    monkeypatch.setenv("DISPATCH_MODE", "fake")
    settings_module.get_settings.cache_clear()

    notification_service = MagicMock()
    provider = MagicMock(return_value=notification_service)
    registry.register(ORDER_CONFIRMATION, provider)
    session = MagicMock()

    try:
        checkout = provide_checkout_service(session)
    finally:
        registry.clear()
        settings_module.get_settings.cache_clear()

    provider.assert_called_once_with(session)
    assert checkout.notification_service is notification_service
    assert checkout.dispatch is not None
