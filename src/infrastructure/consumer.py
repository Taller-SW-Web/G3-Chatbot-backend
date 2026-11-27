"""Outbox consumer entry point (ADR-0030): `python -m src.infrastructure.consumer`.

Runs in its own process (a separate Railway service on the same image), independent of the
API and of `OUTBOX_DISPATCH_MODE`. It needs `RABBITMQ_URL` (fails fast without it), the
database and the same module configuration as the API, because it reuses the handlers of
the checkout and orders modules through the capability registry.
"""
import asyncio
import contextlib
import logging
import os
import signal
import sys
from urllib.parse import urlparse

from src.infrastructure.discovery import configure_modules
from src.shared.infrastructure.broker.consumer import OutboxConsumer
from src.shared.infrastructure.broker.health_server import HealthServer
from src.shared.infrastructure.config.settings import RABBITMQ_URL_SCHEMES, Settings, get_settings
from src.shared.infrastructure.db import connection
from src.shared.infrastructure.di.capabilities import OUTBOX_TASK_HANDLER
from src.shared.infrastructure.di.registry import registry

logger = logging.getLogger("src.infrastructure.consumer")

START_RETRY_MIN = 2.0
START_RETRY_MAX = 30.0


class ConsumerConfigError(RuntimeError):
    """The consumer cannot start with this configuration."""


def validate_settings(settings: Settings) -> None:
    """The consumer needs the broker whatever `OUTBOX_DISPATCH_MODE` says."""
    if not settings.rabbitmq_url:
        raise ConsumerConfigError("RABBITMQ_URL is required to run the outbox consumer")
    if urlparse(settings.rabbitmq_url).scheme not in RABBITMQ_URL_SCHEMES:
        raise ConsumerConfigError("RABBITMQ_URL must use the amqp:// or amqps:// scheme")


def build_consumer(settings: Settings) -> OutboxConsumer:
    configure_modules(registry)
    connection.get_engine()
    return OutboxConsumer(
        settings.rabbitmq_url,
        prefetch=settings.rabbitmq_prefetch,
        session_factory=connection.open_session,
        handler_factory=lambda session: registry.resolve(OUTBOX_TASK_HANDLER, session),
    )


async def _start_with_retry(consumer: OutboxConsumer, stop: asyncio.Event) -> bool:
    """Connects with a bounded backoff while the broker is down; False if stopped first."""
    delay = START_RETRY_MIN
    while not stop.is_set():
        try:
            await consumer.start()
            return True
        except Exception as exc:
            # Class name only: the exception text could carry the broker URL.
            logger.warning("event=consumer_start_failed error=%s retry_in_s=%d", type(exc).__name__, delay)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
        delay = min(START_RETRY_MAX, delay * 2)
    return False


async def run(settings: Settings, port: int) -> int:
    validate_settings(settings)
    consumer = build_consumer(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    health = HealthServer(port, lambda: (consumer.is_healthy, consumer.status()))
    await health.start()
    try:
        if await _start_with_retry(consumer, stop):
            await stop.wait()
            logger.info("event=shutdown_requested")
            await consumer.stop()
    finally:
        await health.stop()
        engine = connection.engine
        if engine is not None:
            await engine.dispose()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    try:
        validate_settings(settings)
    except ConsumerConfigError as exc:
        logger.error("%s", exc)
        return 2
    port = int(os.getenv("PORT", "8000"))
    return asyncio.run(run(settings, port))


if __name__ == "__main__":
    sys.exit(main())
