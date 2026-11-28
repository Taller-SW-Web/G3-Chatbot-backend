"""Promotions list cache (SPEC-08 RNF: 60 s, shared across requests)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.modules.mod_catalog import module as catalog_module
from src.modules.mod_catalog.application.manage_catalog_use_case import (
    PROMOTION_CACHE_TTL_SECONDS,
    ManageCatalogUseCase,
    OptionCache,
)
from src.modules.mod_catalog.module import provide_catalog_service
from src.shared.infrastructure.config import settings as settings_module
from src.shared.infrastructure.di.capabilities import CONVERSATION_CONTEXT
from src.shared.infrastructure.di.registry import registry
from tests.fakes.fake_commerce import FakeConversationContext
from tests.unit.mod_catalog.test_discovery import PROMO, service


def cached_service(cache: OptionCache) -> ManageCatalogUseCase:
    uc, _ = service()
    return ManageCatalogUseCase(
        uc._catalog,
        uc._inventory,
        uc._conversation_context,
        uc._uow,
        option_cache=uc._option_cache,
        promotion_cache=cache,
    )


@pytest.mark.asyncio
async def test_two_promotions_calls_within_ttl_use_one_upstream_request():
    uc = cached_service(OptionCache(PROMOTION_CACHE_TTL_SECONDS))
    uc._catalog.list_promotions = AsyncMock(return_value=[PROMO])

    await uc.promotions()
    await uc.promotions()

    assert uc._catalog.list_promotions.await_count == 1


@pytest.mark.asyncio
async def test_promotions_cache_expires_after_the_ttl():
    cache = OptionCache(PROMOTION_CACHE_TTL_SECONDS)
    uc = cached_service(cache)
    uc._catalog.list_promotions = AsyncMock(return_value=[PROMO])
    await uc.promotions()

    # Age the entry without sleeping: the real 60 s boundary is what matters.
    cached_at, rows = cache._entries["promotions"]
    cache._entries["promotions"] = (
        cached_at - PROMOTION_CACHE_TTL_SECONDS - 1, rows,
    )
    await uc.promotions()

    assert uc._catalog.list_promotions.await_count == 2


@pytest.mark.asyncio
async def test_two_use_cases_share_the_injected_promotions_cache():
    shared = OptionCache(PROMOTION_CACHE_TTL_SECONDS)
    first = cached_service(shared)
    second = cached_service(shared)
    first._catalog.list_promotions = AsyncMock(return_value=[PROMO])
    second._catalog.list_promotions = AsyncMock(return_value=[PROMO])

    await first.promotions()
    await second.promotions()

    assert first._catalog.list_promotions.await_count == 1
    assert second._catalog.list_promotions.await_count == 0


def test_promotions_cache_is_not_shared_by_default():
    first = cached_service(OptionCache(PROMOTION_CACHE_TTL_SECONDS))
    second = cached_service(OptionCache(PROMOTION_CACHE_TTL_SECONDS))

    assert first._promotion_cache is not second._promotion_cache


def test_wiring_injects_the_shared_promotions_cache(monkeypatch):
    monkeypatch.setenv("MODULES_MODE", "fake")
    settings_module.get_settings.cache_clear()
    previous = {name: registry.provider(name) for name in registry.names}
    registry.register(CONVERSATION_CONTEXT, lambda session: FakeConversationContext())
    try:
        first = provide_catalog_service(MagicMock())
        second = provide_catalog_service(MagicMock())

        assert first._promotion_cache is second._promotion_cache
        assert first._promotion_cache is catalog_module._CATALOG_PROMOTION_CACHE
    finally:
        # Restore the process-wide registry instead of clearing capabilities
        # other test modules rely on.
        registry.clear()
        for name, provider in previous.items():
            registry.register(name, provider)
        settings_module.get_settings.cache_clear()
