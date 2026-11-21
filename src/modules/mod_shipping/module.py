"""`mod-shipping` wiring: quotes (G2 Despacho) and saved addresses (G1 Seguridad)."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.mod_shipping import MODULE_ID
from src.modules.mod_shipping.adapters.despacho_client import DespachoClient
from src.modules.mod_shipping.adapters.fake_dispatch import FakeDispatchPort
from src.modules.mod_shipping.adapters.http.shipping_router import create_shipping_router
from src.modules.mod_shipping.application.manage_shipping_use_case import ManageShippingUseCase
from src.shared.infrastructure.auth.providers import (
    provide_security_port,
    provide_service_token_provider,
)
from src.shared.infrastructure.config.settings import Settings, get_settings
from src.shared.infrastructure.db.connection import get_session
from src.shared.infrastructure.di.capabilities import CART_REPOSITORIES, DISPATCH_PORT
from src.shared.infrastructure.di.registry import registry
from src.shared.infrastructure.http.router_options import RouterOptions

# Quotes read the customer's cart through the capability published by `mod-cart`.
REQUIRED_CAPABILITIES = (CART_REPOSITORIES,)


def provide_despacho_client(settings: Settings | None = None):
    """Despacho port honoring `DISPATCH_MODE` (ADR-0022): in-process fake or HTTP.

    The HTTP client authenticates as `modulo-chatbot` with the service token
    provider (ADR-0009); the fake keeps the demo working without G2 credentials.
    """
    settings = settings or get_settings()
    if settings.module_mode("dispatch") == "fake":
        return FakeDispatchPort()
    return DespachoClient(
        base_url=settings.dispatch_base_url,
        timeout_seconds=settings.dispatch_timeout_seconds,
        service_token_provider=provide_service_token_provider(settings),
    )


def provide_shipping_service(
    session: AsyncSession = Depends(get_session),
) -> ManageShippingUseCase:
    """Shipping quote and saved addresses (SPEC-12).

    The cart ports give the lines the client never sends; Seguridad prefill
    addresses and Despacho the real quote with the service token.
    """
    settings = get_settings()
    repositories = registry.resolve(CART_REPOSITORIES, session)
    return ManageShippingUseCase(
        repositories.carts,
        repositories.items,
        provide_despacho_client(settings),
        provide_security_port(settings),
    )


def build_router(options: RouterOptions) -> APIRouter:
    return create_shipping_router(provide_shipping_service)


router = build_router(RouterOptions())


def configure(container) -> None:
    """`mod-orders` reads tracking through this capability (it cannot import us)."""
    container.register(DISPATCH_PORT, provide_despacho_client)


__all__ = [
    "DISPATCH_PORT",
    "MODULE_ID",
    "build_router",
    "configure",
    "provide_despacho_client",
    "provide_shipping_service",
    "router",
]
