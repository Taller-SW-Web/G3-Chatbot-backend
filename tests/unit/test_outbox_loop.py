"""Outbox background loop and FastAPI lifespan (SPEC-15)."""
import asyncio
import logging
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI

from src.infrastructure.worker import outbox_loop
from src.infrastructure.worker.outbox_loop import outbox_worker_lifespan, run_outbox_loop


@asynccontextmanager
async def fake_session():
    yield object()


class Processor:
    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.calls = 0

    async def process_pending_batch(self):
        self.calls += 1
        behavior = self.behaviors.pop(0) if self.behaviors else 0
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


@pytest.mark.asyncio
async def test_loop_survives_a_failed_iteration_and_logs_only_the_error_class(caplog):
    processor = Processor([RuntimeError("Bearer secret-token-123"), 1, 0])
    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(run_outbox_loop(fake_session, lambda _s: processor, 0.01))
        for _ in range(200):
            if processor.calls >= 3:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert processor.calls >= 3
    assert "RuntimeError" in caplog.text
    assert "secret-token-123" not in caplog.text


@pytest.mark.asyncio
async def test_lifespan_starts_and_cancels_the_loop(monkeypatch):
    from src.infrastructure.config import container, settings
    from src.infrastructure.db import connection

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_loop(session_factory, processor_factory, interval):
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(outbox_loop, "run_outbox_loop", fake_loop)
    monkeypatch.setattr(connection, "get_engine", lambda: None)
    monkeypatch.setattr(container, "provide_outbox_processor", lambda session: None)
    monkeypatch.setattr(
        settings, "get_settings", lambda: settings.Settings(outbox_worker_enabled=True, outbox_poll_interval_seconds=1.0)
    )

    async with outbox_worker_lifespan(FastAPI()):
        await asyncio.wait_for(started.wait(), 1)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_lifespan_does_nothing_when_disabled(monkeypatch):
    from src.infrastructure.config import settings

    def boom(*args, **kwargs):
        raise AssertionError("loop must not start")

    monkeypatch.setattr(outbox_loop, "run_outbox_loop", boom)
    monkeypatch.setattr(settings, "get_settings", lambda: settings.Settings(outbox_worker_enabled=False))
    async with outbox_worker_lifespan(FastAPI()):
        pass
