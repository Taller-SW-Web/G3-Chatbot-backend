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

from fastapi import FastAPI

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager]
ProcessorFactory = Callable[[object], object]


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


@asynccontextmanager
async def outbox_worker_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan: starts the loop when OUTBOX_WORKER_ENABLED and cancels it on shutdown."""
    from src.infrastructure.config.container import provide_outbox_processor
    from src.infrastructure.config.settings import get_settings
    from src.infrastructure.db import connection

    settings = get_settings()
    task: asyncio.Task | None = None
    if settings.outbox_worker_enabled:
        connection.get_engine()
        task = asyncio.create_task(
            run_outbox_loop(
                lambda: connection.AsyncSessionLocal(),
                provide_outbox_processor,
                settings.outbox_poll_interval_seconds,
            ),
            name="outbox-worker",
        )
        logger.info("Outbox worker started (every %.1fs)", settings.outbox_poll_interval_seconds)
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            logger.info("Outbox worker stopped")
