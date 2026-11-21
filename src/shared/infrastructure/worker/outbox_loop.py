"""Background loop that runs the outbox worker inside the API process (SPEC-15, ADR-0011).

Each iteration opens its own database session, processes one batch (one commit per job)
and sleeps. A failed iteration is logged and the loop keeps going. Only the exception
class is logged: messages of HTTP/DB errors could echo credentials.
"""
import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

from fastapi import FastAPI

logger = logging.getLogger(__name__)


class BatchProcessor(Protocol):
    """Lo mínimo que el loop necesita del procesador (sin acoplarse a su clase)."""

    async def process_pending_batch(self) -> int: ...


SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]
ProcessorFactory = Callable[[Any], BatchProcessor]


async def run_outbox_loop(
    session_factory: SessionFactory,
    processor_factory: ProcessorFactory,
    interval_seconds: float,
) -> None:
    """Runs until cancelled. Never raises from a failed iteration."""
    while True:
        try:
            async with session_factory() as session:
                processor = processor_factory(session)
                processed = await processor.process_pending_batch()
                if processed:
                    logger.info("Outbox worker processed %d job(s)", processed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Outbox worker iteration failed: %s", type(exc).__name__)
        await asyncio.sleep(interval_seconds)


async def run_attachment_cleanup(
    session_factory: SessionFactory,
    cleanup_factory: Callable[[Any], Any],
    interval_seconds: float,
) -> None:
    """Hourly cleanup of PENDING attachments and orphan files (SPEC-23)."""
    while True:
        try:
            async with session_factory() as session:
                removed = await cleanup_factory(session).cleanup()
                if removed:
                    logger.info("Attachment cleanup removed %d item(s)", removed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Attachment cleanup iteration failed: %s", type(exc).__name__)
        await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def outbox_worker_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan: starts the loop when OUTBOX_WORKER_ENABLED and cancels it on shutdown."""
    from src.shared.infrastructure.config.settings import get_settings
    from src.shared.infrastructure.db import connection
    from src.shared.infrastructure.di.capabilities import ATTACHMENT_CLEANUP
    from src.shared.infrastructure.di.container import provide_outbox_processor
    from src.shared.infrastructure.di.registry import registry

    settings = get_settings()
    task: asyncio.Task | None = None
    cleanup_task: asyncio.Task | None = None
    if settings.outbox_worker_enabled:
        connection.get_engine()
        task = asyncio.create_task(
            run_outbox_loop(
                lambda: connection.open_session(),
                provide_outbox_processor,
                settings.outbox_poll_interval_seconds,
            ),
            name="outbox-worker",
        )
        logger.info("Outbox worker started (every %.1fs)", settings.outbox_poll_interval_seconds)
    if registry.is_registered(ATTACHMENT_CLEANUP):
        connection.get_engine()
        cleanup_task = asyncio.create_task(
            run_attachment_cleanup(
                lambda: connection.open_session(),
                registry.provider(ATTACHMENT_CLEANUP),
                settings.attachment_cleanup_interval_seconds,
            ),
            name="attachment-cleanup",
        )
        logger.info(
            "Attachment cleanup started (every %.0fs)",
            settings.attachment_cleanup_interval_seconds,
        )
    try:
        yield
    finally:
        for name, pending in (("outbox-worker", task), ("attachment-cleanup", cleanup_task)):
            if pending is not None:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
                logger.info("%s stopped", name)
