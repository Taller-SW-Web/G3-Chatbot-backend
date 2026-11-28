"""FastAPI application factory. Run: uvicorn src.infrastructure.main:app

This is the composition root (ADR-0024): it discovers `src.modules.*.module`,
calls each `configure()` once and mounts every `build_router(options)`. Adding a
module never edits this file.
"""
import logging

from fastapi import FastAPI

from src.infrastructure.discovery import configure_modules, discover_modules
from src.shared.infrastructure.config.settings import MODULES_WITH_ADAPTERS, get_settings, warn_unsafe_defaults
from src.shared.infrastructure.di.capabilities import ACTION_SCOPE
from src.shared.infrastructure.di.registry import registry
from src.shared.infrastructure.http.correlation import CorrelationMiddleware
from src.shared.infrastructure.http.dependencies.rate_limiter import provide_rate_limiter
from src.shared.infrastructure.http.gateway_guard import GatewaySecretMiddleware
from src.shared.infrastructure.http.health_router import router as health_router
from src.shared.infrastructure.http.openapi import install_openapi_contract
from src.shared.infrastructure.http.problem import register_problem_handlers
from src.shared.infrastructure.http.router_options import RouterOptions
from src.shared.infrastructure.worker.outbox_loop import outbox_worker_lifespan

API_PREFIX = "/api/v1"

logger = logging.getLogger(__name__)


def create_app(
    turn_scope=None,
    action_scope=None,
) -> FastAPI:
    """Scopes can be replaced by tests behind the text-stream and direct-action endpoints."""
    configure_modules(registry)
    settings = get_settings()
    limiter = provide_rate_limiter()
    if turn_scope is not None:
        limiter.reset()
    warn_unsafe_defaults(settings, logger)
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
    if settings.gateway_shared_secret:
        app.add_middleware(GatewaySecretMiddleware, secret=settings.gateway_shared_secret)
    app.add_middleware(CorrelationMiddleware)
    register_problem_handlers(app)
    install_openapi_contract(app)
    app.include_router(health_router)
    options = RouterOptions(turn_scope=turn_scope, action_scope=action_scope)
    for module in discover_modules():
        app.include_router(module.build_router(options), prefix=API_PREFIX)
    return app


def _default_action_scope():
    """Resolve the capability lazily, after `configure_modules` registered it.

    The composition root knows the capability name, not `mod-chat`; tests that
    pass `action_scope=None` still keep direct actions disabled.
    """
    return registry.resolve(ACTION_SCOPE)


# Production keeps the real action scope; tests pass their own or leave it None.
app = create_app(action_scope=_default_action_scope)


__all__ = ["ACTION_SCOPE", "API_PREFIX", "app", "create_app"]
