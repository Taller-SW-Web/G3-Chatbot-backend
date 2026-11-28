"""Production boot guards and the non-production startup warning (BE-AUD-06/07/14/25/26)."""
import logging

import pytest
from fastapi.testclient import TestClient

from src.infrastructure.main import create_app
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
    "ATTACHMENT_STORAGE": "supabase",
    "SUPABASE_URL": "https://storage.example.com",
    "SUPABASE_SERVICE_KEY": "service-key-value",
    "ALLOWED_ORIGINS": "https://app.example.com",
    "COOKIE_SECURE": "true",
    "PHONE_OTP_SECRET": "otp-secret-value",
    "ATTACHMENT_SIGNING_SECRET": "attachment-secret-value",
    "GATEWAY_SHARED_SECRET": "gateway-secret-value",
    "MODO_QA": "false",
}


@pytest.fixture
def env(monkeypatch):
    get_settings.cache_clear()
    for key in (
        *SAFE_ENV,
        "SECURITY_CLIENT_SECRET",
        "ALLOW_FAKE_SECURITY",
        "ALLOW_FAKE_MODULES",
        "SECURITY_MODE",
        "PRODUCTS_MODE",
        "SALES_MODE",
        "DISPATCH_MODE",
        "REQUIRE_PHONE_VERIFICATION",
        "REQUIRE_VERIFIED_CONTACT",
        "ALLOW_LOCAL_ATTACHMENTS",
        "ATTACHMENT_STORAGE",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_KEY",
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


def test_consumer_role_does_not_require_attachment_settings(env):
    env(
        ATTACHMENT_STORAGE="local",
        ATTACHMENT_SIGNING_SECRET="",
        ATTACHMENT_PUBLIC_BASE_URL="http://localhost:8000/api/v1",
    )
    get_settings.cache_clear()
    settings = get_settings("consumer")
    assert settings.app_role == "consumer"
    assert settings.attachment_storage == "local"

    get_settings.cache_clear()
    with pytest.raises(ValueError, match="ALLOW_LOCAL_ATTACHMENTS"):
        get_settings()


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


def test_production_refuses_local_attachment_storage(env):
    env(ATTACHMENT_STORAGE="local")
    with pytest.raises(ValueError, match="ALLOW_LOCAL_ATTACHMENTS"):
        get_settings()


def test_production_boots_with_local_attachments_only_with_the_exception(env, caplog):
    env(ATTACHMENT_STORAGE="local", ALLOW_LOCAL_ATTACHMENTS="true")
    settings = get_settings()
    assert settings.attachment_storage == "local"
    assert settings.allow_local_attachments is True
    log = logging.getLogger("test.unsafe")
    with caplog.at_level(logging.WARNING, logger="test.unsafe"):
        warn_unsafe_defaults(settings, log)
    message = " ".join(record.getMessage() for record in caplog.records)
    assert "ATTACHMENT_STORAGE=local" in message
    assert "ALLOW_LOCAL_ATTACHMENTS=true" in message


def test_local_attachments_remain_the_development_default(env):
    env(APP_ENV="development", ATTACHMENT_STORAGE="local")
    settings = get_settings()
    assert settings.attachment_storage == "local"
    assert settings.allow_local_attachments is False


def test_startup_refuses_local_attachments_in_production(env):
    env(ATTACHMENT_STORAGE="local")
    with pytest.raises(ValueError, match="ALLOW_LOCAL_ATTACHMENTS"):
        create_app()


def test_startup_boots_with_the_local_attachment_exception(env, caplog):
    env(ATTACHMENT_STORAGE="local", ALLOW_LOCAL_ATTACHMENTS="true")
    with caplog.at_level(logging.WARNING, logger="src.infrastructure.main"):
        create_app()
    message = " ".join(record.getMessage() for record in caplog.records)
    assert "ATTACHMENT_STORAGE=local accepted by ALLOW_LOCAL_ATTACHMENTS=true" in message


def test_production_does_not_fall_back_to_the_client_secret(env, monkeypatch):
    env(PHONE_OTP_SECRET="", ATTACHMENT_SIGNING_SECRET="")
    monkeypatch.setenv("SECURITY_CLIENT_SECRET", "oauth-client-secret")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="PHONE_OTP_SECRET"):
        get_settings()


def test_fake_security_in_production_still_needs_the_opt_in(env, monkeypatch):
    env(SECURITY_MODE="fake")
    with pytest.raises(ValueError, match="ALLOW_FAKE_SECURITY"):
        get_settings()
    monkeypatch.setenv("ALLOW_FAKE_SECURITY", "true")
    get_settings.cache_clear()
    assert get_settings().allow_fake_security is True


@pytest.mark.parametrize(
    "variable",
    ["PRODUCTS_MODE", "SALES_MODE", "DISPATCH_MODE"],
)
def test_production_refuses_each_fake_module_without_the_opt_in(env, variable):
    env(**{variable: "fake"})
    with pytest.raises(ValueError, match="ALLOW_FAKE_MODULES"):
        get_settings()


def test_startup_refuses_fake_modules_in_production(env):
    env(SALES_MODE="fake")
    with pytest.raises(ValueError, match="ALLOW_FAKE_MODULES"):
        create_app()


def test_startup_boots_with_fake_modules_only_with_the_exception(env, caplog):
    env(SALES_MODE="fake", ALLOW_FAKE_MODULES="true")
    assert get_settings().allow_fake_modules is True
    with caplog.at_level(logging.WARNING, logger="src.infrastructure.main"):
        app = create_app()
    message = " ".join(record.getMessage() for record in caplog.records)
    assert "fake modules active" in message
    health = TestClient(app).get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "degraded"
    assert body["fakeModules"] == ["sales"]


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
            Settings(
                app_env="production",
                require_verified_contact=True,
                attachment_storage="supabase",
                supabase_url="https://storage.example.com",
                supabase_service_key="service-key-value",
                gateway_shared_secret="gateway-secret-value",
            ),
            log,
        )
    assert caplog.records == []


def test_production_defaults_enable_the_verified_contact_guard(env, caplog):
    env()
    settings = get_settings()
    assert settings.require_verified_contact is True
    assert settings.require_phone_verification is False
    log = logging.getLogger("test.unsafe")
    with caplog.at_level(logging.WARNING, logger="test.unsafe"):
        warn_unsafe_defaults(settings, log)
    assert caplog.records == []


def test_development_defaults_keep_both_contact_guards_off(env):
    env(APP_ENV="development")
    settings = get_settings()
    assert settings.require_verified_contact is False
    assert settings.require_phone_verification is False


def test_explicit_phone_flag_enables_the_legacy_mode(env):
    env(REQUIRE_PHONE_VERIFICATION="true")
    assert get_settings().require_phone_verification is True


def test_legacy_phone_flag_keeps_the_production_warning_quiet(env, caplog):
    env(REQUIRE_VERIFIED_CONTACT="false", REQUIRE_PHONE_VERIFICATION="true")
    settings = get_settings()
    assert settings.require_verified_contact is False
    assert settings.require_phone_verification is True
    log = logging.getLogger("test.unsafe")
    with caplog.at_level(logging.WARNING, logger="test.unsafe"):
        warn_unsafe_defaults(settings, log)
    assert caplog.records == []


def test_production_warns_when_the_verified_contact_flag_is_disabled(env, caplog):
    env(REQUIRE_VERIFIED_CONTACT="false")
    settings = get_settings()
    assert settings.require_verified_contact is False
    assert settings.require_phone_verification is False
    log = logging.getLogger("test.unsafe")
    with caplog.at_level(logging.WARNING, logger="test.unsafe"):
        warn_unsafe_defaults(settings, log)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "REQUIRE_PHONE_VERIFICATION" in message
    assert "REQUIRE_VERIFIED_CONTACT" in message


def test_verified_contact_flag_is_read_from_the_environment(env, monkeypatch):
    env(APP_ENV="development")
    assert get_settings().require_verified_contact is False
    monkeypatch.setenv("REQUIRE_VERIFIED_CONTACT", "true")
    get_settings.cache_clear()
    assert get_settings().require_verified_contact is True
