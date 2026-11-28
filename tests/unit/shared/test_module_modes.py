"""Per-module fake/http selection: `<MODULE>_MODE` overrides `MODULES_MODE`."""
import pytest

from src.modules.mod_catalog.adapters.fake_products import FakeCatalogPort
from src.modules.mod_catalog.adapters.products_client import ProductsClient
from src.modules.mod_catalog.module import provide_product_catalog
from src.shared.infrastructure.auth.providers import provide_security_port
from src.shared.infrastructure.config.settings import Settings, get_settings
from src.shared.infrastructure.fakes.fake_security import FakeSecurityAdapter
from src.shared.infrastructure.http.security_client import SecurityClient

PRODUCTION_STORAGE = {
    "attachment_storage": "supabase",
    "supabase_url": "https://storage.example.com",
    "supabase_service_key": "service-key-value",
}


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


def test_fake_security_is_refused_in_production_without_opt_in():
    with pytest.raises(ValueError, match="ALLOW_FAKE_SECURITY"):
        Settings(modules_mode="fake", app_env="production")
    with pytest.raises(ValueError, match="fake Seguridad"):
        Settings(modules_mode="http", security_mode="fake", app_env="prod")


def test_fake_security_in_production_needs_the_explicit_opt_in():
    settings = Settings(
        modules_mode="fake",
        app_env="production",
        allow_fake_security=True,
        allow_fake_modules=True,
        **PRODUCTION_STORAGE,
    )
    assert settings.module_mode("security") == "fake"


def test_production_with_fake_products_sales_or_dispatch_needs_allow_fake_modules():
    with pytest.raises(ValueError, match="ALLOW_FAKE_MODULES"):
        Settings(
            modules_mode="fake",
            security_mode="http",
            app_env="production",
            **PRODUCTION_STORAGE,
        )
    settings = Settings(
        modules_mode="fake",
        security_mode="http",
        app_env="production",
        allow_fake_modules=True,
        **PRODUCTION_STORAGE,
    )
    assert settings.module_mode("sales") == "fake"


def test_fake_security_is_allowed_outside_production():
    assert Settings(modules_mode="fake").app_env == "development"
    assert Settings(modules_mode="fake", app_env="staging").module_mode("security") == "fake"


def test_env_reads_app_env_and_opt_in(monkeypatch, clean_settings):
    monkeypatch.setenv("MODULES_MODE", "fake")
    monkeypatch.setenv("APP_ENV", " Production ")
    with pytest.raises(ValueError, match="ALLOW_FAKE_SECURITY"):
        get_settings()
    get_settings.cache_clear()
    # Opt-in parsing only; the other production guards are covered in test_production_settings.py.
    monkeypatch.setattr(
        "src.shared.infrastructure.config.settings._require_production_safe", lambda settings: None
    )
    monkeypatch.setenv("ALLOW_FAKE_SECURITY", "true")
    monkeypatch.setenv("ALLOW_FAKE_MODULES", "true")
    monkeypatch.setenv("ATTACHMENT_STORAGE", "supabase")
    monkeypatch.setenv("SUPABASE_URL", "https://storage.example.com")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-key-value")
    assert get_settings().allow_fake_security is True


def test_unset_app_env_keeps_the_current_railway_config_booting(monkeypatch, clean_settings):
    monkeypatch.setenv("MODULES_MODE", "fake")
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ALLOW_FAKE_SECURITY", raising=False)
    assert get_settings().module_mode("security") == "fake"


def test_startup_builds_with_fake_modules(monkeypatch, clean_settings):
    from src.infrastructure.main import create_app

    monkeypatch.setenv("MODULES_MODE", "fake")
    app = create_app()
    assert app.title == "G3 Chatbot backend"
