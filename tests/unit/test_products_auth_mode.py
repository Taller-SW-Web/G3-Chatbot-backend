"""Explicit supplier authentication through the normal catalog/inventory factories."""
import httpx
import pytest
import respx

from src.adapters.outbound.fakes.fake_products import FakeCatalogPort, FakeInventoryPort
from src.infrastructure.config.container import provide_product_catalog, provide_product_inventory
from src.infrastructure.config.settings import Settings, get_settings
from src.ports.outbound.module_errors import ModuleUnavailableError

G6 = "https://products.example/api/v1"
G7 = "https://security.example/api/v1"


@pytest.mark.parametrize("value", ["auto", "disabled", "", "unknown"])
def test_invalid_auth_selection_fails(monkeypatch, value):
    get_settings.cache_clear()
    monkeypatch.setenv("PRODUCTS_AUTH_MODE", value)
    try:
        with pytest.raises(ValueError, match="PRODUCTS_AUTH_MODE"):
            get_settings()
        with pytest.raises(ValueError, match="PRODUCTS_AUTH_MODE"):
            Settings(products_auth_mode=value)
    finally:
        get_settings.cache_clear()


def test_default_is_service_and_explicit_fake_stays_fake():
    assert Settings().products_auth_mode == "service"
    settings = Settings(products_mode="fake", products_auth_mode="public")
    assert isinstance(provide_product_catalog(settings), FakeCatalogPort)
    assert isinstance(provide_product_inventory(settings), FakeInventoryPort)


@pytest.mark.parametrize("environment", ["prod", "production"])
@pytest.mark.parametrize("allowed", [False, True])
def test_production_public_requires_explicit_allowance(environment, allowed):
    if not allowed:
        with pytest.raises(ValueError, match="ALLOW_PUBLIC_PRODUCTS=true"):
            Settings(app_env=environment, products_auth_mode="public")
    else:
        assert Settings(app_env=environment, products_auth_mode="public",
                        allow_public_products=True).products_auth_mode == "public"
    assert Settings(app_env=environment).products_auth_mode == "service"


@pytest.mark.parametrize("value,allowed", [(None, False), ("false", False), ("0", False),
                                        ("true", True), (" TRUE ", True), ("yes", True), ("1", True)])
def test_public_allowance_environment_parsing(monkeypatch, value, allowed):
    monkeypatch.setenv("APP_ENV", " Production ")
    monkeypatch.setenv("SECURITY_MODE", "http")
    monkeypatch.setenv("PRODUCTS_AUTH_MODE", " PUBLIC ")
    if value is None:
        monkeypatch.delenv("ALLOW_PUBLIC_PRODUCTS", raising=False)
    else:
        monkeypatch.setenv("ALLOW_PUBLIC_PRODUCTS", value)
    get_settings.cache_clear()
    try:
        if allowed:
            assert get_settings().allow_public_products is True
        else:
            with pytest.raises(ValueError, match="ALLOW_PUBLIC_PRODUCTS"):
                get_settings()
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("auth_mode", ["public", "service"])
async def test_normal_factories_send_g6_http_with_selected_auth(auth_mode):
    settings = Settings(products_mode="http", security_mode="http", products_auth_mode=auth_mode,
                        products_base_url=G6, security_base_url=G7,
                        security_client_secret="test-only" if auth_mode == "service" else "")
    token = respx.post(G7 + "/auth/token").mock(return_value=httpx.Response(
        200, json={"access_token": "supplier-token", "expires_in": 300, "token_type": "Bearer"}))
    search = respx.get(G6 + "/productos").mock(return_value=httpx.Response(200, json={"items": []}))
    stock = respx.get(G6 + "/inventario/disponibilidad/comercial").mock(
        return_value=httpx.Response(200, json={"items": [{"sku": "S", "status": "DISPONIBLE"}]}))
    catalog, inventory = provide_product_catalog(settings), provide_product_inventory(settings)
    try:
        assert catalog.timeout_seconds == 4 and inventory.timeout_seconds == 3
        assert await catalog.search_products() == {"items": []}
        assert (await inventory.get_availability(["S"]))[0]["status"] == "DISPONIBLE"
        assert token.called == (auth_mode == "service")
        for route in (search, stock):
            assert route.called
            assert route.calls.last.request.headers.get("Authorization") == (
                "Bearer supplier-token" if auth_mode == "service" else None)
    finally:
        await catalog.aclose()
        await inventory.aclose()


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("status", [401, 403, 500])
@pytest.mark.parametrize("auth_mode", ["public", "service"])
@pytest.mark.parametrize("port", ["catalog", "inventory"])
async def test_supplier_failure_never_switches_auth_or_returns_fake(status, auth_mode, port):
    settings = Settings(products_mode="http", security_mode="http", products_auth_mode=auth_mode,
                        products_base_url=G6, security_base_url=G7, security_client_secret="test-only")
    respx.post(G7 + "/auth/token").mock(return_value=httpx.Response(
        200, json={"access_token": "supplier-token", "expires_in": 300, "token_type": "Bearer"}))
    path = "/productos" if port == "catalog" else "/inventario/disponibilidad/comercial"
    failed = respx.get(G6 + path).mock(return_value=httpx.Response(status))
    client = provide_product_catalog(settings) if port == "catalog" else provide_product_inventory(settings)
    try:
        with pytest.raises(ModuleUnavailableError):
            if port == "catalog":
                await client.search_products()
            else:
                await client.get_availability(["S"])
        assert failed.call_count == 1
        assert failed.calls.last.request.headers.get("Authorization") == (
            "Bearer supplier-token" if auth_mode == "service" else None)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_public_transport_failure_remains_error_without_g7_credentials():
    settings = Settings(products_mode="http", security_mode="http", products_auth_mode="public",
                        products_base_url=G6, security_base_url=G7)
    respx.get(G6 + "/productos").mock(side_effect=httpx.ConnectError("offline"))
    catalog = provide_product_catalog(settings)
    try:
        with pytest.raises(ModuleUnavailableError):
            await catalog.search_products()
    finally:
        await catalog.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_service_token_rejection_never_attempts_public_g6():
    settings = Settings(products_mode="http", security_mode="http", products_base_url=G6,
                        security_base_url=G7, security_client_secret="rejected-test-only")
    token = respx.post(G7 + "/auth/token").mock(return_value=httpx.Response(401, json={"code": "NO_AUTORIZADO"}))
    upstream = respx.get(G6 + "/productos").mock(return_value=httpx.Response(200, json={"items": []}))
    catalog = provide_product_catalog(settings)
    try:
        with pytest.raises(ModuleUnavailableError):
            await catalog.search_products()
        assert token.call_count == 1
        assert not upstream.called
    finally:
        await catalog.aclose()
