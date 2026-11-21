"""`mod-catalog` wiring: Productos (G6) catalog, detail, availability and promotions."""

from functools import lru_cache

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.mod_catalog import MODULE_ID
from src.modules.mod_catalog.adapters.fake_products import FakeCatalogPort, FakeInventoryPort
from src.modules.mod_catalog.adapters.http.catalog_router import create_catalog_router
from src.modules.mod_catalog.adapters.inventory_client import (
    INVENTORY_TIMEOUT_SECONDS,
    InventoryClient,
)
from src.modules.mod_catalog.adapters.products_client import ProductsClient
from src.modules.mod_catalog.application.manage_catalog_use_case import (
    ManageCatalogUseCase,
    OptionCache,
)
from src.shared.domain.module_errors import ModuleUnavailableError
from src.shared.infrastructure.auth.service_token_provider import SecurityServiceTokenProvider
from src.shared.infrastructure.config.settings import Settings, get_settings
from src.shared.infrastructure.db.connection import get_session
from src.shared.infrastructure.http.router_options import RouterOptions
from src.shared.infrastructure.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)
from src.shared.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

# Categories/brands list cache shared across requests (SPEC-06): created once
# by the wiring and injected, so use cases never share a module global.
_CATALOG_OPTION_CACHE = OptionCache()


def provide_product_catalog(settings: Settings | None = None):
    """Catalog port honoring the products mode (`PRODUCTS_MODE`, else `MODULES_MODE`, ADR-0022)."""
    settings = settings or get_settings()
    if settings.module_mode("products") == "fake":
        return FakeCatalogPort()
    return provide_products_client(settings)


def provide_product_inventory(settings: Settings | None = None):
    """Inventory port honoring the products mode (`PRODUCTS_MODE`, else `MODULES_MODE`)."""
    settings = settings or get_settings()
    if settings.module_mode("products") == "fake":
        return FakeInventoryPort()
    return provide_inventory_client(settings)


def provide_products_client(settings: Settings | None = None) -> ProductsClient:
    """Typed Productos catalog client: base URL + 4 s timeout from Settings."""
    settings = settings or get_settings()
    return ProductsClient(
        base_url=settings.products_base_url,
        timeout_seconds=settings.products_timeout_seconds,
        service_token_provider=_products_auth_provider(settings),
    )


def provide_inventory_client(settings: Settings | None = None) -> InventoryClient:
    """Typed Productos inventory client (SPEC-10): live stock, 3 s timeout."""
    settings = settings or get_settings()
    return InventoryClient(
        base_url=settings.products_base_url,
        timeout_seconds=INVENTORY_TIMEOUT_SECONDS,
        service_token_provider=_products_auth_provider(settings),
    )


def provide_catalog_service(
    session: AsyncSession = Depends(get_session),
) -> ManageCatalogUseCase:
    settings = get_settings()
    return ManageCatalogUseCase(
        provide_product_catalog(settings),
        provide_product_inventory(settings),
        ConversationPostgresAdapter(session),
        SqlAlchemyUnitOfWork(session),
        option_cache=_CATALOG_OPTION_CACHE,
    )


def build_router(options: RouterOptions) -> APIRouter:
    return create_catalog_router(provide_catalog_service)


router = build_router(RouterOptions())


def configure(container) -> None:
    """No cross-module capability: catalog publishes its port through `public.py`."""


class _MissingProductsCredential:
    async def get_token(self, *, correlation_id=None, scope=None, audience=None):
        raise ModuleUnavailableError("security", "SECURITY_CLIENT_SECRET required for G6 M2M")


def _products_auth_provider(settings: Settings):
    """Public G6 requires no G7 token; customer authentication is independent."""
    if settings.products_auth_mode == "public":
        return None
    return _products_token_provider(
        settings.security_base_url, settings.security_client_id, settings.security_client_secret,
    )


@lru_cache(maxsize=8)
def _products_token_provider(base_url, client_id, client_secret):
    if not client_secret.strip():
        return _MissingProductsCredential()
    return SecurityServiceTokenProvider(base_url, client_id, client_secret)


__all__ = [
    "MODULE_ID",
    "build_router",
    "configure",
    "provide_catalog_service",
    "provide_inventory_client",
    "provide_product_catalog",
    "provide_product_inventory",
    "provide_products_client",
    "router",
]
