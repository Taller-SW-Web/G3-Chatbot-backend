"""FastAPI application factory. Run: uvicorn src.infrastructure.main:app"""
from fastapi import FastAPI

from src.adapters.inbound.http.chatbot_router import TurnScope, create_chatbot_router
from src.adapters.inbound.http.health_router import router as health_router
from src.adapters.inbound.http.problem import register_problem_handlers
from src.adapters.inbound.http.session_router import create_session_router
from src.infrastructure.config.container import provide_chatbot_service, provide_manage_session, provide_turn_scope
from src.infrastructure.config.settings import get_settings

API_PREFIX = "/api/v1"


def create_app(turn_scope: TurnScope | None = None) -> FastAPI:
    """`turn_scope` lets tests inject a fake use case behind the send endpoint."""
    settings = get_settings()
    app = FastAPI(title="G3 Chatbot backend")
    register_problem_handlers(app)
    app.include_router(health_router)
    app.include_router(
        create_session_router(provide_manage_session, cookie_secure=settings.cookie_secure),
        prefix=API_PREFIX,
    )
    app.include_router(
        create_chatbot_router(
            provide_chatbot_service,
            cookie_secure=settings.cookie_secure,
            turn_scope=turn_scope or provide_turn_scope,
        ),
        prefix=API_PREFIX,
    )
    return app


app = create_app()
