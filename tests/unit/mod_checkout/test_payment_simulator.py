"""Payment simulator: latency flag, wiring and port boundary (SPEC-14 Req. 5, issue #249)."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.modules.mod_checkout import module as checkout_module
from src.modules.mod_checkout.adapters.payment_simulator_adapter import PaymentSimulatorAdapter
from src.modules.mod_checkout.ports.payment_gateway_port import CardValidationError

LATENCY_CARD = "4000 0000 0000 3220"


async def _pay(gateway: PaymentSimulatorAdapter):
    return await gateway.process_payment(LATENCY_CARD, "Juan Perez", "12/40", "123", 10.0)


@pytest.mark.asyncio
async def test_latency_card_waits_five_seconds_only_when_enabled(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    assert (await _pay(PaymentSimulatorAdapter(simulate_latency=True))).result == "APPROVED"
    assert sleeps == [5]

    sleeps.clear()
    assert (await _pay(PaymentSimulatorAdapter(simulate_latency=False))).result == "APPROVED"
    assert sleeps == []


@pytest.mark.asyncio
async def test_gateway_wiring_uses_the_configured_latency_flag(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    settings = SimpleNamespace(payment_simulator_latency_enabled=True)

    await _pay(checkout_module.provide_payment_gateway(settings))
    assert sleeps == [5]

    sleeps.clear()
    settings.payment_simulator_latency_enabled = False
    await _pay(checkout_module.provide_payment_gateway(settings))
    assert sleeps == []


def test_card_validation_error_lives_in_the_port():
    assert CardValidationError.__module__ == "src.modules.mod_checkout.ports.payment_gateway_port"
    assert issubclass(CardValidationError, Exception)


def test_application_does_not_import_adapters():
    application = Path(__file__).resolve().parents[3] / "src" / "modules" / "mod_checkout" / "application"

    offenders = [
        f"{path.relative_to(application)}:{number}"
        for path in application.rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if "adapters" in line
    ]

    assert offenders == []
