"""Seguridad (G1) wiring shared by every module.

The access-token validator, the service-token provider and the security client
are transport-level contracts: any module authenticates the holder or a
machine-to-machine call through them, so they live in the kernel instead of
`mod-auth` (ADR-0024: "auth -> usado por todos vía shared/auth").
"""
from src.shared.application.ports.security_port import SecurityPort
from src.shared.application.ports.token_validator_port import AccessTokenValidatorPort
from src.shared.infrastructure.auth.jwks_token_validator import JwksTokenValidator
from src.shared.infrastructure.auth.service_token_provider import SecurityServiceTokenProvider
from src.shared.infrastructure.config.settings import Settings, get_settings
from src.shared.infrastructure.fakes.fake_security import (
    FakeAccessTokenValidator,
    FakeSecurityAdapter,
    FakeServiceTokenProvider,
)
from src.shared.infrastructure.http.security_client import SecurityClient

# Fake Seguridad state is a singleton on purpose: registrations must survive
# across requests so the demo flow (registro -> verificar -> login) works.
_FAKE_SECURITY = FakeSecurityAdapter()
_FAKE_TOKEN_VALIDATOR = FakeAccessTokenValidator(_FAKE_SECURITY)
_FAKE_SERVICE_TOKEN_PROVIDER = FakeServiceTokenProvider()


def provide_security_client(
    settings: Settings | None = None, client_secret: str | None = None
) -> SecurityClient:
    """Typed Seguridad client (SPEC-01..03): base URL + 5 s timeout from Settings.

    The secret (`SECURITY_CLIENT_SECRET`, A3) only feeds the service token
    provider used by introspection; no other request carries it.
    """
    settings = settings or get_settings()
    secret = (client_secret or settings.security_client_secret).strip()
    service_token_provider = (
        SecurityServiceTokenProvider(
            base_url=settings.security_base_url,
            client_id=settings.security_client_id,
            client_secret=secret,
            timeout_seconds=settings.security_timeout_seconds,
        )
        if secret
        else None
    )
    return SecurityClient(
        base_url=settings.security_base_url,
        timeout_seconds=settings.security_timeout_seconds,
        service_token_provider=service_token_provider,
    )


def provide_security_port(settings: Settings | None = None) -> SecurityPort:
    """Seguridad port honoring `SECURITY_MODE` (else `MODULES_MODE`, ADR-0022)."""
    settings = settings or get_settings()
    if settings.module_mode("security") == "fake":
        return _FAKE_SECURITY
    return provide_security_client(settings)


def provide_access_token_validator(settings: Settings | None = None) -> AccessTokenValidatorPort:
    """Local token validator honoring the security mode: fake claims or JWKS (SPEC-03 Req. 5)."""
    settings = settings or get_settings()
    if settings.module_mode("security") == "fake":
        return _FAKE_TOKEN_VALIDATOR
    return JwksTokenValidator(
        security_base_url=settings.security_base_url,
        expected_issuer=settings.security_expected_issuer,
    )


def provide_service_token_provider(settings: Settings | None = None):
    """Service token provider for privileged calls (A3), following the security mode."""
    settings = settings or get_settings()
    if settings.module_mode("security") == "fake":
        return _FAKE_SERVICE_TOKEN_PROVIDER
    secret = settings.security_client_secret.strip()
    if not secret:
        return None
    return SecurityServiceTokenProvider(
        base_url=settings.security_base_url,
        client_id=settings.security_client_id,
        client_secret=secret,
        timeout_seconds=settings.security_timeout_seconds,
    )
