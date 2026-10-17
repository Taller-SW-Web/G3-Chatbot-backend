"""FastAPI application factory. Run: uvicorn src.infrastructure.main:app"""
import logging

from fastapi import FastAPI

from src.adapters.inbound.http.cart_router import create_cart_router
from src.adapters.inbound.http.catalog_router import create_catalog_router
from src.adapters.inbound.http.chatbot_router import ActionScope, TurnScope, create_chatbot_router
from src.adapters.inbound.http.health_router import router as health_router
from src.adapters.inbound.http.problem import register_problem_handlers
from src.adapters.inbound.http.session_router import create_session_router
from src.infrastructure.config.container import (
    provide_action_scope,
    provide_cart_service,
    provide_catalog_service,
    provide_chatbot_service,
    provide_manage_session,
    provide_turn_scope,
)
from src.infrastructure.config.settings import get_settings

API_PREFIX = "/api/v1"

logger = logging.getLogger(__name__)


def create_app(
    turn_scope: TurnScope | None = None,
    action_scope: ActionScope | None = None,
) -> FastAPI:
    """Scopes can be replaced by tests behind the text-stream and direct-action endpoints."""
    settings = get_settings()
    if settings.modules_mode == "fake":
        logger.warning(
            "MODULES_MODE=fake: using in-process module fakes; "
            "do not deploy this configuration to production."
        )
    app = FastAPI(title="G3 Chatbot backend")
    register_problem_handlers(app)
    app.include_router(health_router)
    app.include_router(
        create_session_router(
            provide_manage_session,
            cookie_secure=settings.cookie_secure,
            allowed_origins=settings.allowed_origins,
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
    return app


app = create_app(action_scope=provide_action_scope)
