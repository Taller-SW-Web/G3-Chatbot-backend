"""Composition of the consumer process: fail-fast configuration and signal-driven shutdown."""
import asyncio
import os
import signal
from unittest.mock import AsyncMock

import pytest

from src.infrastructure import consumer as entrypoint
from src.shared.infrastructure.config.settings import Settings


def test_it_fails_fast_without_rabbitmq_url():
    with pytest.raises(entrypoint.ConsumerConfigError, match="RABBITMQ_URL"):
        entrypoint.validate_settings(Settings(rabbitmq_url=""))


def test_it_rejects_a_non_amqp_url_without_echoing_it():
    with pytest.raises(entrypoint.ConsumerConfigError) as raised:
        entrypoint.validate_settings(Settings(rabbitmq_url="http://user:s3cret@host/"))
    assert "s3cret" not in str(raised.value)


@pytest.mark.parametrize("mode", ["direct", "broker"])
def test_it_does_not_depend_on_the_dispatch_mode(mode):
    entrypoint.validate_settings(
        Settings(rabbitmq_url="amqp://guest:guest@localhost/", outbox_dispatch_mode=mode)
    )


def test_consumer_role_skips_attachment_storage_guards():
    """#222: the outbox worker does not use AttachmentStorage."""
    consumer = Settings(
        app_env="production",
        app_role="consumer",
        modules_mode="http",
        attachment_storage="local",
    )
    assert consumer.attachment_storage == "local"

    with pytest.raises(ValueError, match="ALLOW_LOCAL_ATTACHMENTS"):
        Settings(
            app_env="production",
            app_role="api",
            modules_mode="http",
            attachment_storage="local",
        )


def test_main_exits_2_when_the_url_is_missing(monkeypatch, caplog):
    monkeypatch.setattr(entrypoint, "get_settings", lambda app_role="api": Settings(rabbitmq_url=""))
    assert entrypoint.main() == 2
    assert "RABBITMQ_URL" in caplog.text


@pytest.fixture
def wired(monkeypatch):
    consumer = AsyncMock()
    consumer.is_healthy = True
    consumer.status = lambda: {"status": "ok"}
    monkeypatch.setattr(entrypoint, "build_consumer", lambda settings: consumer)
    monkeypatch.setattr(entrypoint.connection, "engine", None)
    return consumer


@pytest.mark.asyncio
async def test_sigterm_stops_the_consumer_and_returns_cleanly(wired):
    settings = Settings(rabbitmq_url="amqp://guest:guest@localhost/")
    running = asyncio.create_task(entrypoint.run(settings, 0))
    await asyncio.sleep(0.05)
    wired.start.assert_awaited_once()

    os.kill(os.getpid(), signal.SIGTERM)

    assert await asyncio.wait_for(running, timeout=5) == 0
    wired.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_broker_that_is_down_at_boot_is_retried_until_it_is_up(wired, monkeypatch):
    monkeypatch.setattr(entrypoint, "START_RETRY_MIN", 0.01)
    wired.start.side_effect = [ConnectionError("amqp://u:s3cret@h/"), None]
    stop = asyncio.Event()

    assert await entrypoint._start_with_retry(wired, stop) is True
    assert wired.start.await_count == 2


@pytest.mark.asyncio
async def test_the_start_retry_stops_when_shutdown_is_requested(wired, caplog):
    wired.start.side_effect = ConnectionError("amqp://u:s3cret@h/")
    stop = asyncio.Event()
    stop.set()
    assert await entrypoint._start_with_retry(wired, stop) is False
    assert "s3cret" not in caplog.text
