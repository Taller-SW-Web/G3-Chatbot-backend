"""FastAPI application factory. Run: uvicorn src.infrastructure.main:app"""
import logging

from fastapi import FastAPI

from src.adapters.inbound.http.chatbot_router import ActionScope, TurnScope, create_chatbot_router
from src.infrastructure.config.container import (
    provide_action_scope,
    provide_chatbot_service,
    provide_turn_scope,
)
from src.infrastructure.discovery import configure_modules
from src.modules.mod_auth.module import router as auth_router
from src.modules.mod_cart.module import router as cart_router
from src.modules.mod_catalog.module import router as catalog_router
from src.modules.mod_checkout.module import router as checkout_router
from src.modules.mod_orders.module import router as orders_router
from src.modules.mod_postventa.module import router as postventa_router
from src.modules.mod_shipping.module import router as shipping_router
from src.shared.infrastructure.config.settings import MODULES_WITH_ADAPTERS, get_settings
from src.shared.infrastructure.di.registry import registry
from src.shared.infrastructure.http.correlation import CorrelationMiddleware
from src.shared.infrastructure.http.dependencies.rate_limiter import provide_rate_limiter
from src.shared.infrastructure.http.health_router import router as health_router
from src.shared.infrastructure.http.problem import register_problem_handlers
from src.shared.infrastructure.worker.outbox_loop import outbox_worker_lifespan

API_PREFIX = "/api/v1"

logger = logging.getLogger(__name__)


def create_app(
    turn_scope: TurnScope | None = None,
    action_scope: ActionScope | None = None,
) -> FastAPI:
    """Scopes can be replaced by tests behind the text-stream and direct-action endpoints."""
    configure_modules(registry)
    settings = get_settings()
    limiter = provide_rate_limiter()
    if turn_scope is not None:
        limiter.reset()
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
    app = FastAPI(title="G3 Chatbot backend", lifespan=outbox_worker_lifespan)
    app.add_middleware(CorrelationMiddleware)
    register_problem_handlers(app)
    app.include_router(health_router)
    app.include_router(auth_router, prefix=API_PREFIX)
    app.include_router(catalog_router, prefix=API_PREFIX)
    app.include_router(cart_router, prefix=API_PREFIX)
    app.include_router(
        create_chatbot_router(
            provide_chatbot_service,
            cookie_secure=settings.cookie_secure,
            turn_scope=turn_scope or provide_turn_scope,
            action_scope=action_scope,
            rate_limiter=limiter,
        ),
        prefix=API_PREFIX,
    )
    app.include_router(orders_router, prefix=API_PREFIX)
    app.include_router(postventa_router, prefix=API_PREFIX)
    app.include_router(checkout_router, prefix=API_PREFIX)
    app.include_router(shipping_router, prefix=API_PREFIX)
    return app


app = create_app(action_scope=provide_action_scope)
