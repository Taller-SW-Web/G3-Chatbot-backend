"""`mod-cart` wiring: customer/anonymous carts and the cart repositories.

The repositories are exposed as a tiny bundle because checkout and shipping
need the concrete rows (state changes and lines), not a use case.
"""
from dataclasses import dataclass

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.mod_cart.adapters.cart_item_postgres_adapter import CartItemPostgresAdapter
from src.modules.mod_cart.adapters.cart_postgres_adapter import CartPostgresAdapter
from src.modules.mod_cart.adapters.http.cart_router import create_cart_router
from src.modules.mod_cart.application.manage_cart_use_case import ManageCartUseCase
from src.modules.mod_catalog.public import (
    provide_catalog_service,
    provide_product_catalog,
    provide_product_inventory,
)
from src.shared.infrastructure.config.settings import get_settings
from src.shared.infrastructure.db.connection import get_session
from src.shared.infrastructure.http.router_options import RouterOptions
from src.shared.infrastructure.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)
from src.shared.infrastructure.persistence.message_postgres_adapter import MessagePostgresAdapter
from src.shared.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

CART_REPOSITORIES = "cart_repositories"


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
        ConversationPostgresAdapter(session),
        MessagePostgresAdapter(session),
        SqlAlchemyUnitOfWork(session),
        discovery=provide_catalog_service(session),
    )


def build_router(options: RouterOptions) -> APIRouter:
    return create_cart_router(provide_cart_service)


router = build_router(RouterOptions())


def configure(container) -> None:
    """Shipping depends on no module but needs the cart lines: expose them by capability."""
    container.register(CART_REPOSITORIES, provide_cart_repositories)
