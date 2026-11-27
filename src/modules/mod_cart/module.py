"""`mod-cart` wiring: customer/anonymous carts and the cart repositories.

The repositories are exposed as a tiny bundle because checkout and shipping
need the concrete rows (state changes and lines), not a use case.
"""
from dataclasses import dataclass

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.mod_cart import MODULE_ID
from src.modules.mod_cart.adapters.cart_item_postgres_adapter import CartItemPostgresAdapter
from src.modules.mod_cart.adapters.cart_postgres_adapter import CartPostgresAdapter
from src.modules.mod_cart.adapters.http.cart_router import create_cart_router
from src.modules.mod_cart.application.manage_cart_use_case import ManageCartUseCase
from src.modules.mod_cart.application.merge_anonymous_cart_service import (
    MergeAnonymousCartService,
)
from src.modules.mod_catalog.public import (
    provide_catalog_service,
    provide_product_catalog,
    provide_product_inventory,
)
from src.shared.infrastructure.config.settings import get_settings
from src.shared.infrastructure.db.connection import get_session
from src.shared.infrastructure.di.capabilities import CART_MERGE, CONVERSATION_CONTEXT
from src.shared.infrastructure.di.registry import registry
from src.shared.infrastructure.http.router_options import RouterOptions
from src.shared.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

# Cart memory and audit rows live in chat; without its port the cart is unusable.
REQUIRED_CAPABILITIES = (CONVERSATION_CONTEXT,)


@dataclass(frozen=True)
class CartRepositories:
    carts: CartPostgresAdapter
    items: CartItemPostgresAdapter


def provide_cart_repositories(session: AsyncSession) -> CartRepositories:
    return CartRepositories(carts=CartPostgresAdapter(session), items=CartItemPostgresAdapter(session))


def provide_cart_service(
    session: AsyncSession = Depends(get_session),
) -> ManageCartUseCase:
    settings = get_settings()
    return ManageCartUseCase(
        CartPostgresAdapter(session),
        CartItemPostgresAdapter(session),
        provide_product_catalog(settings),
        provide_product_inventory(settings),
        registry.resolve(CONVERSATION_CONTEXT, session),
        SqlAlchemyUnitOfWork(session),
        discovery=provide_catalog_service(session),
    )


def provide_cart_merge(session: AsyncSession) -> MergeAnonymousCartService:
    settings = get_settings()
    return MergeAnonymousCartService(
        carts=CartPostgresAdapter(session),
        items=CartItemPostgresAdapter(session),
        inventory=provide_product_inventory(settings),
    )


def build_router(options: RouterOptions) -> APIRouter:
    return create_cart_router(provide_cart_service)


router = build_router(RouterOptions())


def configure(container) -> None:
    """Registers `CART_MERGE` so `mod-auth` can fuse anonymous carts without importing `mod-cart`."""
    container.register(CART_MERGE, provide_cart_merge)


__all__ = [
    "MODULE_ID",
    "REQUIRED_CAPABILITIES",
    "build_router",
    "configure",
    "provide_cart_merge",
    "provide_cart_repositories",
    "provide_cart_service",
    "router",
]
