"""Postventa draft purge scheduled from the shared worker (SPEC-19/21, #265)."""
import asyncio
import logging
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI

from src.shared.domain.request_context import correlation_id_var
from src.shared.infrastructure.worker import outbox_loop
from src.shared.infrastructure.worker.outbox_loop import (
    outbox_worker_lifespan,
    run_postventa_draft_purge,
)


@asynccontextmanager
async def fake_session():
    yield object()


class Purger:
    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.calls = 0
        self.correlations: list[str | None] = []

    async def run(self):
        self.calls += 1
        self.correlations.append(correlation_id_var.get())
        behavior = self.behaviors.pop(0) if self.behaviors else 0
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


@pytest.mark.asyncio
async def test_purge_loop_runs_periodically_with_a_run_id_and_survives_failures(caplog):
    purger = Purger([RuntimeError("password=hunter2"), (1, 2), (0, 0)])
    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(
            run_postventa_draft_purge(fake_session, lambda _s: purger, 0.01)
        )
        for _ in range(200):
            if purger.calls >= 3:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert purger.calls >= 3
    # One correlation id per run, always present, never reused across iterations.
    assert all(purger.correlations)
    assert len(set(purger.correlations)) == len(purger.correlations)
    assert "RuntimeError" in caplog.text
    assert "correlation_id=" in caplog.text
    assert "hunter2" not in caplog.text


@pytest.mark.asyncio
async def test_lifespan_starts_and_cancels_the_postventa_purge(monkeypatch):
    from src.shared.infrastructure.config import settings
    from src.shared.infrastructure.db import connection
    from src.shared.infrastructure.di import container
    from src.shared.infrastructure.di.capabilities import POSTVENTA_DRAFT_PURGE
    from src.shared.infrastructure.di.registry import registry

    started = asyncio.Event()
    cancelled = asyncio.Event()
    observed: list[float] = []

    async def fake_purge_loop(session_factory, purge_factory, interval):
        observed.append(interval)
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def fake_outbox_loop(session_factory, processor_factory, interval):
        await asyncio.sleep(3600)

    monkeypatch.setattr(outbox_loop, "run_postventa_draft_purge", fake_purge_loop)
    monkeypatch.setattr(outbox_loop, "run_outbox_loop", fake_outbox_loop)
    monkeypatch.setattr(connection, "get_engine", lambda: None)
    monkeypatch.setattr(container, "provide_outbox_processor", lambda session: None)
    monkeypatch.setattr(
        settings,
        "get_settings",
        lambda: settings.Settings(
            outbox_worker_enabled=True,
            outbox_poll_interval_seconds=1.0,
            postventa_draft_purge_interval_seconds=1800.0,
        ),
    )
    monkeypatch.setattr(
        registry,
        "_providers",
        {**registry._providers, POSTVENTA_DRAFT_PURGE: lambda session: object()},
    )

    async with outbox_worker_lifespan(FastAPI()):
        await asyncio.wait_for(started.wait(), 1)

    assert cancelled.is_set()
    assert observed == [1800.0]


def test_interval_is_read_from_the_environment(monkeypatch):
    from src.shared.infrastructure.config.settings import _build_settings

    monkeypatch.setenv("POSTVENTA_DRAFT_PURGE_INTERVAL_SECONDS", "900")
    assert _build_settings().postventa_draft_purge_interval_seconds == 900.0


def test_interval_below_one_minute_is_rejected(monkeypatch):
    from src.shared.infrastructure.config.settings import _build_settings

    monkeypatch.setenv("POSTVENTA_DRAFT_PURGE_INTERVAL_SECONDS", "30")
    with pytest.raises(ValueError, match=">= 60"):
        _build_settings()

