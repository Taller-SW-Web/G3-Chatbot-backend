"""Per-module fake/http selection: `<MODULE>_MODE` overrides `MODULES_MODE`."""
import pytest

from src.adapters.outbound.fakes.fake_products import FakeCatalogPort
from src.adapters.outbound.fakes.fake_security import FakeSecurityAdapter
from src.adapters.outbound.http_clients.products_client import ProductsClient
from src.adapters.outbound.http_clients.security_client import SecurityClient
from src.infrastructure.config.container import provide_product_catalog, provide_security_port
from src.infrastructure.config.settings import Settings, get_settings


def test_modules_inherit_the_global_mode_without_overrides():
    settings = Settings(modules_mode="fake")
    assert all(settings.module_mode(m) == "fake" for m in ("security", "products", "sales", "dispatch"))
    assert settings.fake_modules() == ("security", "products", "sales", "dispatch")


def test_override_switches_only_its_module():
    settings = Settings(modules_mode="fake", security_mode="http")
    assert settings.module_mode("security") == "http"
    assert settings.module_mode("products") == "fake"
    assert isinstance(provide_security_port(settings), SecurityClient)
    assert isinstance(provide_product_catalog(settings), FakeCatalogPort)


def test_override_can_fake_one_module_in_http_mode():
    settings = Settings(modules_mode="http", security_mode="fake")
    assert isinstance(provide_security_port(settings), FakeSecurityAdapter)
    assert isinstance(provide_product_catalog(settings), ProductsClient)
    assert settings.fake_modules() == ("security",)


def test_unknown_module_is_rejected():
    with pytest.raises(ValueError, match="unknown module"):
        Settings().module_mode("billing")


@pytest.fixture
def clean_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_env_overrides_are_read_and_empty_inherits(monkeypatch, clean_settings):
    monkeypatch.setenv("MODULES_MODE", "fake")
    monkeypatch.setenv("SECURITY_MODE", " HTTP ")
    monkeypatch.setenv("PRODUCTS_MODE", "")
    settings = get_settings()
    assert settings.module_mode("security") == "http"
    assert settings.module_mode("products") == "fake"


def test_invalid_env_override_fails_loudly(monkeypatch, clean_settings):
    monkeypatch.setenv("SALES_MODE", "mock")
    with pytest.raises(ValueError, match="SALES_MODE must be fake or http"):
        get_settings()
