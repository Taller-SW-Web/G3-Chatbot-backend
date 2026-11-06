"""FastAPI application factory. Run: uvicorn src.infrastructure.main:app"""
import logging

from fastapi import FastAPI

from src.adapters.inbound.http.cart_router import create_cart_router
from src.adapters.inbound.http.catalog_router import create_catalog_router
from src.adapters.inbound.http.chatbot_router import ActionScope, TurnScope, create_chatbot_router
from src.adapters.inbound.http.checkout_router import create_checkout_router
from src.adapters.inbound.http.health_router import router as health_router
from src.adapters.inbound.http.orders_router import create_orders_router
from src.adapters.inbound.http.problem import register_problem_handlers
from src.adapters.inbound.http.session_router import create_session_router
from src.infrastructure.config.container import (
    provide_action_scope,
    provide_cart_service,
    provide_catalog_service,
    provide_chatbot_service,
    provide_checkout_service,
    provide_manage_session,
    provide_orders_service,
    provide_rate_limiter,
    provide_turn_scope,
)
from src.infrastructure.config.settings import MODULES_WITH_ADAPTERS, get_settings

API_PREFIX = "/api/v1"

logger = logging.getLogger(__name__)


def create_app(
    turn_scope: TurnScope | None = None,
    action_scope: ActionScope | None = None,
) -> FastAPI:
    """Scopes can be replaced by tests behind the text-stream and direct-action endpoints."""
    settings = get_settings()
    fake_modules = tuple(m for m in settings.fake_modules() if m in MODULES_WITH_ADAPTERS)
    if fake_modules:
        logger.warning(
            "In-process module fakes active for %s (MODULES_MODE / <MODULE>_MODE); "
            "real integrations are not used for them.",
            ", ".join(fake_modules),
        )
    if "security" in fake_modules:
        logger.warning(
            "Fake Seguridad accepts any credentials (APP_ENV=%s, ALLOW_FAKE_SECURITY=%s).",
            settings.app_env,
            settings.allow_fake_security,
        )
    logger.warning(
        "No HTTP adapter exists yet for sales and dispatch (SALES_MODE / DISPATCH_MODE "
        "have no effect); the real modules are not called."
    )
    app = FastAPI(title="G3 Chatbot backend")
    register_problem_handlers(app)
    app.include_router(health_router)
    app.include_router(
        create_session_router(
            provide_manage_session,
            cookie_secure=settings.cookie_secure,
            allowed_origins=settings.allowed_origins,
            rate_limiter=provide_rate_limiter(),
        ),
        prefix=API_PREFIX,
    )
    app.include_router(
        create_chatbot_router(
            provide_chatbot_service,
            cookie_secure=settings.cookie_secure,
            turn_scope=turn_scope or provide_turn_scope,
            action_scope=action_scope,
        ),
        prefix=API_PREFIX,
    )
    app.include_router(
        create_catalog_router(provide_catalog_service), prefix=API_PREFIX
    )
    app.include_router(create_cart_router(provide_cart_service), prefix=API_PREFIX)
    app.include_router(create_orders_router(provide_orders_service), prefix=API_PREFIX)
    app.include_router(create_checkout_router(provide_checkout_service), prefix=API_PREFIX)
    return app


app = create_app(action_scope=provide_action_scope)
