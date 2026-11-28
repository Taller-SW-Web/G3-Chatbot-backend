"""`mod-auth` wiring: customer session service (SPEC-01..04).

The router factory and the use-case provider live here; `main.py` only mounts
`build_router(options)`.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.mod_auth import MODULE_ID
from src.modules.mod_auth.adapters.http.phone_router import create_phone_router
from src.modules.mod_auth.adapters.http.session_router import create_session_router
from src.modules.mod_auth.adapters.simulated_sms_sender import SimulatedSmsSender
from src.modules.mod_auth.application.manage_phone_verification_use_case import (
    ManagePhoneVerificationUseCase,
)
from src.modules.mod_auth.application.manage_session_use_case import ManageSessionUseCase
from src.shared.application.ports.cart_merge_port import CartMergeResult
from src.shared.infrastructure.auth.providers import (
    provide_access_token_validator,
    provide_security_port,
)
from src.shared.infrastructure.config.settings import get_settings
from src.shared.infrastructure.db.connection import get_session
from src.shared.infrastructure.di.capabilities import CART_MERGE, CHECKOUT_DOCUMENT
from src.shared.infrastructure.di.registry import registry
from src.shared.infrastructure.http.dependencies.rate_limiter import provide_rate_limiter
from src.shared.infrastructure.http.router_options import RouterOptions
from src.shared.infrastructure.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)
from src.shared.infrastructure.persistence.local_phone_verification_postgres_adapter import (
    LocalPhoneVerificationPostgresAdapter,
)
from src.shared.infrastructure.persistence.phone_otp_postgres_adapter import PhoneOtpPostgresAdapter
from src.shared.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

# Capabilities registered by other modules; optional because auth is the base of
# the dependency graph and must serve sessions without them. The composition
# root logs their absence and `/health` reports it (H-08).
OPTIONAL_CAPABILITIES = (CART_MERGE, CHECKOUT_DOCUMENT)


class _NoCheckoutDocument:
    """Explicit null object: the profile omits `ultimoDocumento`, never a silent None."""

    async def latest_document(self, customer_id):
        return None


class _NoCartMerge:
    """Explicit null object: login opens the session and reports no merged cart."""

    async def merge_for_login(self, anonymous_sid, customer_id) -> CartMergeResult:
        return CartMergeResult(merged=False)


def provide_manage_session(session: AsyncSession = Depends(get_session)) -> ManageSessionUseCase:
    """Customer session service (SPEC-03): orchestration over the Seguridad port.

    The local token validator enforces the `CLIENTE` role before a session is
    opened; the persistence ports feed `celularVerificado` / `ultimoDocumento`.
    `CART_MERGE` is resolved per request so `mod-cart` registers its provider
    without auth importing the cart module (ADR-0024).
    """
    settings = get_settings()
    checkouts = (
        registry.resolve(CHECKOUT_DOCUMENT, session)
        if registry.is_registered(CHECKOUT_DOCUMENT)
        else _NoCheckoutDocument()
    )
    cart_merge = (
        registry.resolve(CART_MERGE, session)
        if registry.is_registered(CART_MERGE)
        else _NoCartMerge()
    )
    return ManageSessionUseCase(
        security=provide_security_port(settings),
        validator=provide_access_token_validator(settings),
        phone_verifications=LocalPhoneVerificationPostgresAdapter(session),
        checkouts=checkouts,
        conversations=ConversationPostgresAdapter(session),
        cart_merge=cart_merge,
        uow=SqlAlchemyUnitOfWork(session),
    )


def build_router(options: RouterOptions) -> APIRouter:
    settings = get_settings()
    router = APIRouter()
    router.include_router(
        create_session_router(
            provide_manage_session,
            cookie_secure=settings.cookie_secure,
            allowed_origins=settings.allowed_origins,
            rate_limiter=provide_rate_limiter(),
        )
    )
    router.include_router(create_phone_router(provide_phone_service))
    return router


def provide_phone_service(
    session: AsyncSession = Depends(get_session),
) -> ManagePhoneVerificationUseCase:
    """Local OTP verification (SPEC-04), independent from Seguridad."""
    settings = get_settings()
    return ManagePhoneVerificationUseCase(
        security=provide_security_port(settings),
        otp_store=PhoneOtpPostgresAdapter(session),
        sms=SimulatedSmsSender(log_code=settings.app_env not in ("production", "prod")),
        verifications=LocalPhoneVerificationPostgresAdapter(session),
        uow=SqlAlchemyUnitOfWork(session),
        secret=settings.phone_otp_secret,
        ttl_seconds=settings.phone_otp_ttl_seconds,
        max_attempts=settings.phone_otp_max_attempts,
        max_sends=settings.phone_otp_max_sends,
        send_window_seconds=settings.phone_otp_send_window_seconds,
        modo_qa=settings.modo_qa,
    )


router = build_router(RouterOptions())


def configure(container) -> None:
    """No cross-module capability: auth is the base of the dependency graph."""


__all__ = [
    "MODULE_ID",
    "OPTIONAL_CAPABILITIES",
    "build_router",
    "configure",
    "provide_manage_session",
    "provide_phone_service",
    "router",
]
