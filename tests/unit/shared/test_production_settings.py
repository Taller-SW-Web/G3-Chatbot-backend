"""Production boot guards and the non-production startup warning (BE-AUD-06/07/14/25/26)."""
import logging

import pytest

from src.shared.infrastructure.config.settings import Settings, get_settings, warn_unsafe_defaults

SAFE_ENV = {
    "APP_ENV": "production",
    "MODULES_MODE": "http",
    "SECURITY_BASE_URL": "https://auth.example.com/api/v1",
    "SECURITY_EXPECTED_ISSUER": "https://auth.example.com/api/v1/auth",
    "PRODUCTS_BASE_URL": "https://products.example.com",
    "SALES_BASE_URL": "https://sales.example.com",
    "POSTVENTA_BASE_URL": "https://postventa.example.com",
    "DISPATCH_BASE_URL": "https://dispatch.example.com",
    "FRONTEND_BASE_URL": "https://app.example.com",
    "ATTACHMENT_PUBLIC_BASE_URL": "https://api.example.com/api/v1",
    "ALLOWED_ORIGINS": "https://app.example.com",
    "COOKIE_SECURE": "true",
    "PHONE_OTP_SECRET": "otp-secret-value",
    "ATTACHMENT_SIGNING_SECRET": "attachment-secret-value",
    "MODO_QA": "false",
}


@pytest.fixture
def env(monkeypatch):
    get_settings.cache_clear()
    for key in (
        *SAFE_ENV,
        "SECURITY_CLIENT_SECRET",
        "ALLOW_FAKE_SECURITY",
        "SECURITY_MODE",
        "REQUIRE_PHONE_VERIFICATION",
        "REQUIRE_VERIFIED_CONTACT",
    ):
        monkeypatch.delenv(key, raising=False)

    def apply(**overrides):
        for key, value in {**SAFE_ENV, **overrides}.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


def test_fully_configured_production_boots(env):
    env()
    assert get_settings().is_production
    assert get_settings().unsafe_defaults() == []


@pytest.mark.parametrize(
    "overrides,fragment",
    [
        ({"MODO_QA": "true"}, "MODO_QA"),
        ({"PHONE_OTP_SECRET": ""}, "PHONE_OTP_SECRET"),
        ({"ATTACHMENT_SIGNING_SECRET": ""}, "ATTACHMENT_SIGNING_SECRET"),
        ({"COOKIE_SECURE": "false"}, "COOKIE_SECURE"),
        ({"SECURITY_EXPECTED_ISSUER": ""}, "SECURITY_EXPECTED_ISSUER"),
        ({"FRONTEND_BASE_URL": "http://localhost:3000"}, "FRONTEND_BASE_URL"),
        ({"ATTACHMENT_PUBLIC_BASE_URL": "http://localhost:8000/api/v1"}, "ATTACHMENT_PUBLIC_BASE_URL"),
        ({"ALLOWED_ORIGINS": "http://localhost:3000"}, "ALLOWED_ORIGINS"),
        ({"SALES_BASE_URL": "http://localhost:4012"}, "SALES_BASE_URL"),
    ],
)
def test_production_refuses_unsafe_setting(env, overrides, fragment):
    env(**overrides)
    with pytest.raises(ValueError, match=fragment):
        get_settings()


def test_production_does_not_fall_back_to_the_client_secret(env, monkeypatch):
    env(PHONE_OTP_SECRET="", ATTACHMENT_SIGNING_SECRET="")
    monkeypatch.setenv("SECURITY_CLIENT_SECRET", "oauth-client-secret")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="PHONE_OTP_SECRET"):
        get_settings()


def test_fake_security_in_production_still_needs_the_opt_in(env, monkeypatch):
    env(MODULES_MODE="fake")
    with pytest.raises(ValueError, match="ALLOW_FAKE_SECURITY"):
        get_settings()
    monkeypatch.setenv("ALLOW_FAKE_SECURITY", "true")
    get_settings.cache_clear()
    assert get_settings().allow_fake_security is True


def test_unset_app_env_boots_with_dev_defaults(env, monkeypatch):
    env(MODULES_MODE="fake", MODO_QA="true", COOKIE_SECURE="false")
    monkeypatch.delenv("APP_ENV")
    get_settings.cache_clear()
    assert not get_settings().is_production


def test_non_production_logs_a_single_warning_with_every_unsafe_default(caplog):
    settings = Settings(modules_mode="fake", app_env="staging", modo_qa=True, cookie_secure=False)
    log = logging.getLogger("test.unsafe")
    with caplog.at_level(logging.WARNING, logger="test.unsafe"):
        warn_unsafe_defaults(settings, log)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    for fragment in ("MODO_QA", "COOKIE_SECURE", "PHONE_OTP_SECRET", "localhost", "fake Seguridad"):
        assert fragment in message


def test_no_warning_in_production_or_when_nothing_is_unsafe(caplog):
    log = logging.getLogger("test.unsafe")
    with caplog.at_level(logging.WARNING, logger="test.unsafe"):
        warn_unsafe_defaults(
            Settings(app_env="production", require_verified_contact=True), log
        )
    assert caplog.records == []


def test_production_warns_when_no_contact_guard_is_enabled(caplog):
    log = logging.getLogger("test.unsafe")
    settings = Settings(app_env="production")
    with caplog.at_level(logging.WARNING, logger="test.unsafe"):
        warn_unsafe_defaults(settings, log)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "REQUIRE_PHONE_VERIFICATION" in message
    assert "REQUIRE_VERIFIED_CONTACT" in message


def test_verified_contact_flag_is_read_from_the_environment(env, monkeypatch):
    env()
    assert get_settings().require_verified_contact is False
    monkeypatch.setenv("REQUIRE_VERIFIED_CONTACT", "true")
    get_settings.cache_clear()
    assert get_settings().require_verified_contact is True
