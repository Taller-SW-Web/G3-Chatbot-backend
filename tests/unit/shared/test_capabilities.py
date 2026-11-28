"""Capability contract: missing required ones abort startup, optional ones degrade."""
import asyncio
from types import SimpleNamespace

import pytest

from src.infrastructure import discovery
from src.shared.infrastructure.di.registry import CapabilityNotRegistered, registry
from src.shared.infrastructure.http.health_router import health


def _fake_module(**extra):
    return SimpleNamespace(
        __name__="src.modules.mod_fake.module",
        MODULE_ID="mod-fake",
        configure=lambda container: None,
        **extra,
    )


def test_required_capability_missing_fails_startup(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "discover_modules",
        lambda: [_fake_module(REQUIRED_CAPABILITIES=("not_registered",))],
    )
    registry.clear()

    with pytest.raises(CapabilityNotRegistered, match="not_registered"):
        discovery.configure_modules(registry)


def test_optional_capability_missing_is_reported_by_health(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "discover_modules",
        lambda: [_fake_module(OPTIONAL_CAPABILITIES=("optional_absent",))],
    )
    registry.clear()

    discovery.configure_modules(registry)

    body = asyncio.run(health())
    assert body["status"] == "degraded"
    assert body["missingCapabilities"] == ["optional_absent"]
    assert body["emailMode"] in ("smtp", "fake")
    registry.clear()


def test_module_without_configure_fails_loudly(monkeypatch):
    module = _fake_module()
    del module.configure
    monkeypatch.setattr(discovery, "discover_modules", lambda: [module])
    registry.clear()

    with pytest.raises(CapabilityNotRegistered, match="configure"):
        discovery.configure_modules(registry)
