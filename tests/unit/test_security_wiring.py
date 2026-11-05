"""Seguridad wiring: fake/http selection and the process-wide rate limiter (ADR-0022)."""
from src.adapters.inbound.http.dependencies.rate_limiter import InMemoryRateLimiter
from src.adapters.outbound.auth.jwks_token_validator import JwksTokenValidator
from src.adapters.outbound.auth.service_token_provider import SecurityServiceTokenProvider
from src.adapters.outbound.fakes.fake_security import (
    FakeAccessTokenValidator,
    FakeSecurityAdapter,
    FakeServiceTokenProvider,
)
from src.adapters.outbound.http_clients.security_client import SecurityClient
from src.infrastructure.config.container import (
    provide_access_token_validator,
    provide_rate_limiter,
    provide_security_port,
    provide_service_token_provider,
)
from src.infrastructure.config.settings import Settings


def test_fake_mode_returns_in_process_security_ports():
    settings = Settings(modules_mode="fake")
    security = provide_security_port(settings)
    assert isinstance(security, FakeSecurityAdapter)
    assert isinstance(provide_access_token_validator(settings), FakeAccessTokenValidator)
    assert isinstance(provide_service_token_provider(settings), FakeServiceTokenProvider)
    # Same instance across calls: registrations must survive between requests.
    assert provide_security_port(settings) is security


def test_http_mode_returns_typed_security_adapters():
    settings = Settings(modules_mode="http", security_client_secret="wired-secret")
    assert isinstance(provide_security_port(settings), SecurityClient)
    assert isinstance(provide_access_token_validator(settings), JwksTokenValidator)
    assert isinstance(provide_service_token_provider(settings), SecurityServiceTokenProvider)


def test_http_mode_builds_the_security_client_without_a_client_secret():
    # Customer endpoints do not need the secret; only introspection does.
    settings = Settings(modules_mode="http", security_client_secret="")
    security = provide_security_port(settings)
    assert isinstance(security, SecurityClient)
    assert security._service_tokens is None


def test_rate_limiter_is_a_process_singleton():
    assert provide_rate_limiter() is provide_rate_limiter()
    assert isinstance(provide_rate_limiter(), InMemoryRateLimiter)


def test_expected_issuer_setting_reaches_the_token_validator(monkeypatch):
    from src.infrastructure.config.settings import get_settings

    monkeypatch.setenv("SECURITY_EXPECTED_ISSUER", "https://issuer.example/api/v1/auth")
    get_settings.cache_clear()
    try:
        settings = get_settings()
    finally:
        get_settings.cache_clear()
    assert settings.security_expected_issuer == "https://issuer.example/api/v1/auth"
    validator = provide_access_token_validator(Settings(modules_mode="http", security_expected_issuer=settings.security_expected_issuer))
    assert validator._expected_issuer == "https://issuer.example/api/v1/auth"
    assert Settings().security_expected_issuer == "auth-service"
