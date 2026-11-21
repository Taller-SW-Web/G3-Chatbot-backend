"""Outbox background loop and FastAPI lifespan (SPEC-15)."""
import asyncio
import logging
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from sqlalchemy.exc import DBAPIError

from src.shared.infrastructure.worker import outbox_loop
from src.shared.infrastructure.worker.outbox_loop import outbox_worker_lifespan, run_outbox_loop


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


class DriverCheckViolationError(Exception):
    """Stands in for the asyncpg error chained under the SQLAlchemy dialect error."""


class AdaptedIntegrityError(Exception):
    """Stands in for SQLAlchemy's asyncpg dialect error (exposes sqlstate)."""

    sqlstate = "23514"


def _dbapi_error_with_secret():
    orig = AdaptedIntegrityError('new row violates check constraint, password=hunter2')
    orig.__cause__ = DriverCheckViolationError("password=hunter2")
    return DBAPIError("INSERT INTO outbox VALUES (%(token)s)", {"token": "secret-token-123"}, orig)


@pytest.mark.asyncio
async def test_loop_logs_driver_error_class_and_sqlstate_without_messages(caplog):
    processor = Processor([_dbapi_error_with_secret(), 0])
    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(run_outbox_loop(fake_session, lambda _s: processor, 0.01))
        for _ in range(200):
            if processor.calls >= 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "DBAPIError" in caplog.text
    assert "AdaptedIntegrityError" in caplog.text
    assert "DriverCheckViolationError" in caplog.text
    assert "sqlstate=23514" in caplog.text
    assert "hunter2" not in caplog.text
    assert "secret-token-123" not in caplog.text


@pytest.mark.asyncio
async def test_lifespan_starts_and_cancels_the_loop(monkeypatch):
    from src.shared.infrastructure.config import settings
    from src.shared.infrastructure.db import connection
    from src.shared.infrastructure.di import container

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
    from src.shared.infrastructure.config import settings

    def boom(*args, **kwargs):
        raise AssertionError("loop must not start")

    monkeypatch.setattr(outbox_loop, "run_outbox_loop", boom)
    monkeypatch.setattr(settings, "get_settings", lambda: settings.Settings(outbox_worker_enabled=False))
    async with outbox_worker_lifespan(FastAPI()):
        pass


class Expirer:
    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.calls = 0

    async def expire_due_checkouts(self):
        self.calls += 1
        behavior = self.behaviors.pop(0) if self.behaviors else 0
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


@pytest.mark.asyncio
async def test_checkout_expiry_loop_survives_failures_and_keeps_sweeping(caplog):
    expirer = Expirer([RuntimeError("boom"), 2, 0])
    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(
            outbox_loop.run_checkout_expiry(fake_session, lambda _s: expirer, 0.01)
        )
        for _ in range(200):
            if expirer.calls >= 3:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert expirer.calls >= 3
    assert "expired 2 checkout" in caplog.text
