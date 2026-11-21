"""`mod-auth` wiring: customer session service (SPEC-01..04).

The router factory and the use-case provider live here; `main.py` only mounts
`build_router(options)`.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.mod_auth import MODULE_ID
from src.modules.mod_auth.adapters.http.session_router import create_session_router
from src.modules.mod_auth.application.manage_session_use_case import ManageSessionUseCase
from src.shared.infrastructure.auth.providers import (
    provide_access_token_validator,
    provide_security_port,
)
from src.shared.infrastructure.config.settings import get_settings
from src.shared.infrastructure.db.connection import get_session
from src.shared.infrastructure.di.capabilities import CHECKOUT_DOCUMENT
from src.shared.infrastructure.di.registry import registry
from src.shared.infrastructure.http.dependencies.rate_limiter import provide_rate_limiter
from src.shared.infrastructure.http.router_options import RouterOptions
from src.shared.infrastructure.persistence.local_phone_verification_postgres_adapter import (
    LocalPhoneVerificationPostgresAdapter,
)

# Capability registered by `mod-checkout`; optional because auth is the base of
# the dependency graph and must still serve the profile without a checkout
# document. The composition root logs it and `/health` reports it (H-08).
OPTIONAL_CAPABILITIES = (CHECKOUT_DOCUMENT,)


class _NoCheckoutDocument:
    """Explicit null object: the profile omits `ultimoDocumento`, never a silent None."""

    async def latest_document(self, customer_id):
        return None


def provide_manage_session(session: AsyncSession = Depends(get_session)) -> ManageSessionUseCase:
    """Customer session service (SPEC-03): orchestration over the Seguridad port.

    The local token validator enforces the `CLIENTE` role before a session is
    opened; the persistence ports feed `celularVerificado` / `ultimoDocumento`.
    """
    settings = get_settings()
    checkouts = (
        registry.resolve(CHECKOUT_DOCUMENT, session)
        if registry.is_registered(CHECKOUT_DOCUMENT)
        else _NoCheckoutDocument()
    )
    return ManageSessionUseCase(
        security=provide_security_port(settings),
        validator=provide_access_token_validator(settings),
        phone_verifications=LocalPhoneVerificationPostgresAdapter(session),
        checkouts=checkouts,
    )


def build_router(options: RouterOptions) -> APIRouter:
    settings = get_settings()
    return create_session_router(
        provide_manage_session,
        cookie_secure=settings.cookie_secure,
        allowed_origins=settings.allowed_origins,
        rate_limiter=provide_rate_limiter(),
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
    "router",
]
