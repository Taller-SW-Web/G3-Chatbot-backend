"""Real-time latency card of the payment simulator (SPEC-14 Req. 5, issue #249).

No PostgreSQL needed: it covers settings -> module wiring -> adapter and measures
the wall clock so a regression to a no-op sleep fails.
"""
import time

import pytest

from src.modules.mod_checkout.module import provide_payment_gateway
from src.shared.infrastructure.config.settings import Settings, get_settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

LATENCY_CARD = "4000 0000 0000 3220"


async def _pay(gateway):
    return await gateway.process_payment(LATENCY_CARD, "Juan Perez", "12/40", "123", 10.0)


def _production_settings() -> Settings:
    """Production defaults without booting the app (no env fallbacks involved)."""
    return Settings(
        app_env="production",
        attachment_storage="supabase",
        supabase_url="https://storage.example.com",
        supabase_service_key="service-key-value",
    )


async def test_latency_card_takes_at_least_five_seconds_with_the_flag_on(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("PAYMENT_SIMULATOR_LATENCY_ENABLED", raising=False)
    get_settings.cache_clear()
    try:
        started = time.monotonic()
        result = await _pay(provide_payment_gateway())
        elapsed = time.monotonic() - started
    finally:
        get_settings.cache_clear()

    assert result.result == "APPROVED"
    assert elapsed >= 5.0


async def test_production_keeps_the_latency_flag_off():
    settings = _production_settings()
    assert settings.payment_simulator_latency_enabled is False

    started = time.monotonic()
    result = await _pay(provide_payment_gateway(settings))
    elapsed = time.monotonic() - started

    assert result.result == "APPROVED"
    assert elapsed < 5.0
