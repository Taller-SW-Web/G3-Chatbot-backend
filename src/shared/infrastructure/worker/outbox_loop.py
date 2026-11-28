"""Background loop that runs the outbox worker inside the API process (SPEC-15, ADR-0011).

Each iteration opens its own database session, processes one batch (one commit per job)
and sleeps. A failed iteration is logged and the loop keeps going. Only exception
classes and the SQLSTATE code are logged: messages of HTTP/DB errors could echo
credentials or bound parameters.
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


def describe_error(exc: BaseException) -> str:
    """Safe summary: exception class, wrapped driver error classes and SQLSTATE, never messages."""
    parts = [type(exc).__name__]
    orig = getattr(exc, "orig", None)
    if orig is not None:
        parts.append(type(orig).__name__)
        cause = orig.__cause__
        if cause is not None:
            parts.append(type(cause).__name__)
        sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        if sqlstate:
            parts.append(f"sqlstate={sqlstate}")
    return " / ".join(parts)


# Must match the default `limit` of `OutboxProcessor.process_pending_batch`: a batch that
# comes back full means more rows are waiting, so the loop runs the next one at once.
OUTBOX_BATCH_SIZE = 20
# Upper bound of back-to-back batches per cycle, so a long backlog still yields to the poll.
OUTBOX_MAX_BATCHES_PER_CYCLE = 50


async def run_outbox_loop(
    session_factory: SessionFactory,
    processor_factory: ProcessorFactory,
    interval_seconds: float,
) -> None:
    """Runs until cancelled. Never raises from a failed iteration.

    Each cycle drains the outbox while batches come back full (bounded), then sleeps.
    Sleeping after every batch capped the relay at 20 rows per poll interval (~4/s with
    the 5 s default), which fell behind a burst of payments in the H5-P5 k6 run.
    """
    while True:
        try:
            processed = 0
            for _ in range(OUTBOX_MAX_BATCHES_PER_CYCLE):
                async with session_factory() as session:
                    processor = processor_factory(session)
                    batch = await processor.process_pending_batch()
                processed += batch
                if batch < OUTBOX_BATCH_SIZE:
                    break
            if processed:
                logger.info("Outbox worker processed %d job(s)", processed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Outbox worker iteration failed: %s", describe_error(exc))
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


async def run_checkout_expiry(
    session_factory: SessionFactory,
    expirer_factory: Callable[[Any], Any],
    interval_seconds: float,
) -> None:
    """Periodic `ExpirarCheckouts` sweep (SPEC-14): expires overdue checkouts, never dies."""
    while True:
        try:
            async with session_factory() as session:
                expired = await expirer_factory(session).expire_due_checkouts()
                if expired:
                    logger.info("Checkout expiry sweep expired %d checkout(s)", expired)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Checkout expiry iteration failed: %s", describe_error(exc))
        await asyncio.sleep(interval_seconds)


async def run_conversation_retention(
    session_factory: SessionFactory,
    retention_factory: Callable[[Any], Any],
    interval_seconds: float,
) -> None:
    """Periodic conversation retention sweep (SPEC-05): closes >7d anonymous, archives >90d customer."""
    while True:
        try:
            async with session_factory() as session:
                closed, archived = await retention_factory(session).run()
                if closed or archived:
                    logger.info(
                        "Conversation retention closed=%d archived=%d",
                        closed,
                        archived,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Conversation retention iteration failed: %s", describe_error(exc))
        await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def outbox_worker_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan: starts the loop when OUTBOX_WORKER_ENABLED and cancels it on shutdown."""
    from src.shared.infrastructure.config.settings import get_settings
    from src.shared.infrastructure.db import connection
    from src.shared.infrastructure.di.capabilities import (
        ATTACHMENT_CLEANUP,
        CHECKOUT_EXPIRY,
        CONVERSATION_RETENTION,
        OUTBOX_PUBLISHER,
    )
    from src.shared.infrastructure.di.container import provide_outbox_processor
    from src.shared.infrastructure.di.registry import registry

    settings = get_settings()
    task: asyncio.Task | None = None
    publisher = None
    cleanup_task: asyncio.Task | None = None
    expiry_task: asyncio.Task | None = None
    retention_task: asyncio.Task | None = None
    try:
        if settings.outbox_worker_enabled and settings.outbox_dispatch_mode == "broker":
            # One long-lived connection for the whole process (ADR-0030); the URL is never logged.
            from src.shared.infrastructure.broker.rabbitmq_outbox_publisher import (
                RabbitMqOutboxPublisher,
            )

            publisher = RabbitMqOutboxPublisher(settings.rabbitmq_url)
            await publisher.start()
            registry.register(OUTBOX_PUBLISHER, lambda: publisher)
            logger.info("Outbox dispatch mode: broker")
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
        if settings.outbox_worker_enabled and registry.is_registered(ATTACHMENT_CLEANUP):
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
        if settings.outbox_worker_enabled and registry.is_registered(CHECKOUT_EXPIRY):
            connection.get_engine()
            expiry_task = asyncio.create_task(
                run_checkout_expiry(
                    lambda: connection.open_session(),
                    registry.provider(CHECKOUT_EXPIRY),
                    settings.checkout_expiry_interval_seconds,
                ),
                name="checkout-expiry",
            )
            logger.info(
                "Checkout expiry sweeper started (every %.0fs)", settings.checkout_expiry_interval_seconds
            )
        if settings.outbox_worker_enabled and registry.is_registered(CONVERSATION_RETENTION):
            connection.get_engine()
            retention_task = asyncio.create_task(
                run_conversation_retention(
                    lambda: connection.open_session(),
                    registry.provider(CONVERSATION_RETENTION),
                    settings.conversation_retention_interval_seconds,
                ),
                name="conversation-retention",
            )
            logger.info(
                "Conversation retention started (every %.0fs)",
                settings.conversation_retention_interval_seconds,
            )
        yield
    finally:
        for name, pending in (
            ("outbox-worker", task),
            ("attachment-cleanup", cleanup_task),
            ("checkout-expiry", expiry_task),
            ("conversation-retention", retention_task),
        ):
            if pending is not None:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
                logger.info("%s stopped", name)
        if publisher is not None:
            await publisher.close()
            logger.info("RabbitMQ relay connection closed")
