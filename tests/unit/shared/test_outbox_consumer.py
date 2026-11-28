"""Outbox consumer (ADR-0030): ack ordering, retry routing, DLQ, no-ack on DB failure, health."""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aio_pika.exceptions import AMQPConnectionError, DeliveryError
from pamqp.commands import Basic

from src.shared.domain.module_errors import ModuleRequestError, ModuleUnavailableError
from src.shared.infrastructure.broker import consumer as consumer_module
from src.shared.infrastructure.broker.consumer import OutboxConsumer, is_retryable
from src.shared.infrastructure.broker.topology import (
    DEAD_LETTER_EXCHANGE,
    QUEUE_NOTIFICATIONS,
    QUEUE_SALES,
    QUEUES,
)
from src.shared.infrastructure.worker.outbox_handler import NonRetryableTaskError
from tests.fakes.fake_broker import FakeIncomingMessage, FakePublishChannel, unroutable_error

JOB_ID = "5b1c3a52-0d6e-4f64-9a41-0c8f0d3a7e11"
SALES = next(s for s in QUEUES if s.name == QUEUE_SALES)
NOTIFICATIONS = next(s for s in QUEUES if s.name == QUEUE_NOTIFICATIONS)


class FakeSession:
    def __init__(self, events, commit_error=None):
        self.events = events
        self.commit_error = commit_error

    async def commit(self):
        if self.commit_error:
            raise self.commit_error
        self.events.append("commit")


class Harness:
    """Consumer with fake session, handler and publish channel; nothing touches a broker."""

    def __init__(self, monkeypatch):
        self.events: list[str] = []
        self.channel = FakePublishChannel(events=self.events)
        self.handler = AsyncMock()
        self.mark_failed = AsyncMock()
        self.session_error: Exception | None = None
        self.commit_error: Exception | None = None
        self.sessions_opened = 0

        @asynccontextmanager
        async def session_factory():
            if self.session_error:
                raise self.session_error
            self.sessions_opened += 1
            yield FakeSession(self.events, self.commit_error)

        async def declare(channel):
            self.events.append("declare_topology")

        monkeypatch.setattr(consumer_module, "declare_topology", declare)
        monkeypatch.setattr(
            consumer_module,
            "OutboxPostgresAdapter",
            lambda session: SimpleNamespace(marcar_fallido=self._marcar_fallido),
        )
        self.consumer = OutboxConsumer(
            "amqp://user:s3cret@broker.invalid/",
            prefetch=7,
            session_factory=session_factory,
            handler_factory=lambda session: SimpleNamespace(dispatch=self._dispatch),
            requeue_delay=0,
        )
        self.consumer._publish_channel = self.channel

    async def _dispatch(self, *args, **kwargs):
        self.events.append("handle")
        return await self.handler(*args, **kwargs)

    async def _marcar_fallido(self, job_id, last_error):
        self.events.append("marcar_fallido")
        return await self.mark_failed(job_id, last_error)

    def message(self, *args, **kwargs):
        return FakeIncomingMessage(*args, events=self.events, **kwargs)


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


# -- success --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_runs_the_handler_commits_and_only_then_acks(h):
    message = h.message("NOTIFY_SALES_PAYMENT", {"order_id": "PED-1"})
    await h.consumer._on_message(SALES, message)

    assert h.events == ["handle", "commit", "ack"]
    args, kwargs = h.handler.await_args
    assert args[1:] == ("NOTIFY_SALES_PAYMENT", {"order_id": "PED-1"})
    assert str(args[0]) == JOB_ID and kwargs == {"relayed": True}
    assert h.consumer.stats.consumed == 1
    assert h.channel.published == []


@pytest.mark.asyncio
async def test_a_failed_commit_is_a_retryable_failure_and_is_not_acked_before_the_republish(h):
    h.commit_error = RuntimeError("db down")
    await h.consumer._on_message(SALES, h.message())
    assert h.events == ["handle", f"publish:default:{SALES.retry_queue(timedelta(seconds=5))}", "ack"]


# -- retry routing -------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "outbox_type", "attempt", "queue_suffix"),
    [
        (SALES, "NOTIFY_SALES_PAYMENT", 1, "retry.5s"),
        (SALES, "REQUEST_CANCELLATION", 2, "retry.15s"),
        (SALES, "NOTIFY_SALES_PAYMENT", 3, "retry.45s"),
        (SALES, "NOTIFY_SALES_PAYMENT", 4, "retry.2m"),
        (SALES, "NOTIFY_SALES_PAYMENT", 5, "retry.5m"),
        (NOTIFICATIONS, "SEND_EMAIL", 1, "retry.1m"),
        (NOTIFICATIONS, "SEND_EMAIL", 2, "retry.5m"),
        (NOTIFICATIONS, "SEND_FEEDBACK_EMAIL", 3, "retry.15m"),
    ],
)
async def test_a_transient_failure_goes_to_the_retry_queue_of_its_attempt(
    h, spec, outbox_type, attempt, queue_suffix
):
    h.handler.side_effect = ModuleUnavailableError("ventas", "timeout")
    message = h.message(outbox_type, attempt=attempt)

    await h.consumer._on_message(spec, message)

    (published,) = h.channel.published
    assert (published.exchange, published.routing_key) == ("", f"{spec.name}.{queue_suffix}")
    assert published.attempt == attempt + 1
    assert published.mandatory is True
    assert published.message.body == message.body
    assert published.message.message_id == JOB_ID
    assert published.message.headers["type"] == outbox_type
    # confirm first, ack second; the outbox row is not touched
    assert h.events[-2:] == [f"publish:default:{spec.name}.{queue_suffix}", "ack"]
    h.mark_failed.assert_not_awaited()
    assert h.consumer.stats.retried == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 404, 422, 500])
async def test_any_http_error_other_than_400_and_409_is_retryable(h, status):
    h.handler.side_effect = ModuleRequestError("ventas", status, "X")
    await h.consumer._on_message(SALES, h.message())
    assert h.consumer.stats.retried == 1 and h.consumer.stats.dead_lettered == 0


def test_the_retry_rule_matches_the_direct_worker():
    assert not is_retryable(ModuleRequestError("ventas", 400, "VALIDACION"))
    assert not is_retryable(ModuleRequestError("ventas", 409, "CONFLICTO"))
    assert not is_retryable(NonRetryableTaskError("bad"))
    assert is_retryable(ModuleRequestError("ventas", 503, "X"))
    assert is_retryable(ValueError("anything else"))


# -- DLQ -----------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 409])
async def test_a_permanent_error_marks_failed_dead_letters_and_acks(h, status):
    h.handler.side_effect = ModuleRequestError("ventas", status, "CONFLICTO", "already cancelled")
    await h.consumer._on_message(SALES, h.message("REQUEST_CANCELLATION", attempt=2))

    assert h.events[-4:] == ["marcar_fallido", "commit", f"publish:{DEAD_LETTER_EXCHANGE}:chatbot.ventas.dlq", "ack"]
    job_id, last_error = h.mark_failed.await_args.args
    assert str(job_id) == JOB_ID
    assert last_error.startswith("PERMANENT_ERROR: ") and str(status) in last_error
    (published,) = h.channel.published
    assert (published.exchange, published.routing_key) == (DEAD_LETTER_EXCHANGE, "chatbot.ventas.dlq")
    assert published.attempt == 2
    assert published.message.headers["x-source-queue"] == QUEUE_SALES
    assert "already cancelled" in published.message.headers["x-failure-reason"]
    assert (h.consumer.stats.dead_lettered, h.consumer.stats.failed) == (1, 1)
    assert h.consumer.stats.retried == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "outbox_type", "attempt", "dlq"),
    [
        (SALES, "NOTIFY_SALES_PAYMENT", 6, "chatbot.ventas.dlq"),
        (NOTIFICATIONS, "SEND_EMAIL", 4, "chatbot.notificaciones.dlq"),
    ],
)
async def test_exhausted_attempts_are_dead_lettered_not_retried(h, spec, outbox_type, attempt, dlq):
    h.handler.side_effect = ModuleUnavailableError("ventas", "timeout")
    await h.consumer._on_message(spec, h.message(outbox_type, attempt=attempt))

    (published,) = h.channel.published
    assert (published.exchange, published.routing_key) == (DEAD_LETTER_EXCHANGE, dlq)
    assert h.mark_failed.await_args.args[1].startswith("RETRIES_EXHAUSTED: ")
    assert h.events[-1] == "ack"


@pytest.mark.asyncio
async def test_a_message_without_a_valid_type_header_is_dead_lettered_without_calling_the_handler(h):
    await h.consumer._on_message(SALES, h.message(outbox_type=None))
    h.handler.assert_not_awaited()
    assert h.channel.published[0].exchange == DEAD_LETTER_EXCHANGE
    h.mark_failed.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not json", b'["a list"]'])
async def test_a_malformed_body_is_dead_lettered(h, body):
    await h.consumer._on_message(SALES, h.message(body=body))
    h.handler.assert_not_awaited()
    assert h.channel.published[0].exchange == DEAD_LETTER_EXCHANGE


@pytest.mark.asyncio
async def test_a_message_without_a_row_id_is_dead_lettered_without_a_db_write(h):
    await h.consumer._on_message(SALES, h.message(outbox_type=None, message_id="not-a-uuid"))
    h.mark_failed.assert_not_awaited()
    assert h.channel.published[0].exchange == DEAD_LETTER_EXCHANGE
    assert h.events[-1] == "ack"


# -- no ack when a write fails ---------------------------------------------------------


@pytest.mark.asyncio
async def test_when_marcar_fallido_fails_the_message_is_requeued_not_acked_nor_published(h):
    h.handler.side_effect = ModuleRequestError("ventas", 409, "CONFLICTO")
    h.mark_failed.side_effect = RuntimeError("db down")

    message = h.message()
    await h.consumer._on_message(SALES, message)

    assert "ack" not in h.events
    assert h.events[-1] == "nack(requeue=True)"
    assert h.channel.published == []
    assert h.consumer.stats.dead_lettered == 0


@pytest.mark.asyncio
async def test_when_the_session_cannot_be_opened_for_marcar_fallido_the_message_is_requeued(h):
    h.handler.side_effect = ModuleRequestError("ventas", 400, "VALIDACION")
    original_marcar = h.mark_failed

    async def explode(*args):
        raise AMQPConnectionError("irrelevant")

    original_marcar.side_effect = explode
    await h.consumer._on_message(SALES, h.message())
    assert h.events[-1] == "nack(requeue=True)"


@pytest.mark.asyncio
async def test_an_unconfirmed_retry_publish_requeues_the_original(h):
    h.handler.side_effect = ModuleUnavailableError("ventas", "timeout")
    h.channel.errors.append(DeliveryError(None, Basic.Nack(delivery_tag=1)))

    await h.consumer._on_message(SALES, h.message())

    assert "ack" not in h.events and h.events[-1] == "nack(requeue=True)"
    assert h.consumer.stats.retried == 0


@pytest.mark.asyncio
async def test_an_unconfirmed_dlx_publish_requeues_the_original_after_marking_failed(h):
    h.handler.side_effect = ModuleRequestError("ventas", 409, "CONFLICTO")
    h.channel.errors.append(DeliveryError(None, Basic.Nack(delivery_tag=1)))

    await h.consumer._on_message(SALES, h.message())

    assert "marcar_fallido" in h.events
    assert "ack" not in h.events and h.events[-1] == "nack(requeue=True)"


@pytest.mark.asyncio
async def test_an_unroutable_republication_redeclares_the_topology_once_and_retries(h):
    h.handler.side_effect = ModuleUnavailableError("ventas", "timeout")
    h.channel.errors.append(unroutable_error())

    await h.consumer._on_message(SALES, h.message())

    assert h.events.count("declare_topology") == 1
    assert len(h.channel.published) == 1 and h.events[-1] == "ack"


# -- idempotent redelivery ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_redelivered_message_is_handled_again_with_the_same_effects_and_acked(h):
    first = h.message("NOTIFY_SALES_PAYMENT", {"order_id": "PED-1"})
    again = h.message("NOTIFY_SALES_PAYMENT", {"order_id": "PED-1"})
    again.redelivered = True

    await h.consumer._on_message(SALES, first)
    await h.consumer._on_message(SALES, again)

    # The handler is what makes the second pass a no-op (see test_outbox_handler.py);
    # here we assert the consumer neither retries nor dead-letters a redelivery.
    assert h.handler.await_count == 2
    assert h.events.count("ack") == 2 and h.channel.published == []
    assert h.consumer.stats.consumed == 2


@pytest.mark.asyncio
async def test_the_consumer_runs_the_shared_handler_payment_rules_like_the_direct_worker(h):
    """Broker mode: the real handler on a relayed PAGADO answer confirms once, on ANULADO never."""
    from unittest.mock import AsyncMock as Mock

    from src.shared.infrastructure.worker.outbox_handler import OutboxTaskHandler

    sales, order_ref, notifications = Mock(), Mock(), Mock()
    handler = OutboxTaskHandler(Mock(), order_ref, sales, notifications)
    h.consumer._handler_factory = lambda session: handler
    body = {"order_id": "PED-1", "payment_data": {"transaccionId": "T-1"}}

    sales.notify_payment.return_value = {"nuevoEstado": "PAGADO"}
    await h.consumer._on_message(SALES, h.message("NOTIFY_SALES_PAYMENT", body))
    order_ref.actualizar_estado.assert_awaited_once_with("PED-1", "PAID_NOTIFIED")
    notifications.enqueue_confirmation.assert_awaited_once_with("PED-1")

    order_ref.actualizar_estado.reset_mock()
    notifications.enqueue_confirmation.reset_mock()
    sales.notify_payment.return_value = {"nuevoEstado": "ANULADO"}
    await h.consumer._on_message(SALES, h.message("NOTIFY_SALES_PAYMENT", body))
    order_ref.actualizar_estado.assert_awaited_once_with("PED-1", "CANCELLED")
    notifications.enqueue_confirmation.assert_not_awaited()
    assert h.events.count("ack") == 2 and h.channel.published == []


# -- failures while settling ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lost_channel_on_ack_is_tolerated_and_not_counted(h):
    message = h.message()
    message.ack_error = RuntimeError("channel closed")
    await h.consumer._on_message(SALES, message)
    assert h.consumer.stats.consumed == 0


@pytest.mark.asyncio
async def test_logs_never_contain_the_broker_url(h, caplog):
    h.handler.side_effect = RuntimeError("could not reach amqp://user:s3cret@broker.invalid/")
    with caplog.at_level(logging.INFO):
        await h.consumer._on_message(SALES, h.message(attempt=9))
    assert "s3cret" not in caplog.text and "broker.invalid" not in caplog.text
    assert "event=dead_lettered" in caplog.text


# -- health and shutdown --------------------------------------------------------------


class FakeQueue:
    def __init__(self, name):
        self.name = name
        self.cancelled = []
        self.tracked = False  # True once declared on the (robust) channel
        self.live = {}  # the channel's registered consumers (aiormq `channel.consumers`)

    async def consume(self, callback, no_ack=False):
        self.callback = callback
        assert no_ack is False
        self.live[f"tag-{self.name}"] = callback
        return f"tag-{self.name}"

    async def cancel(self, tag):
        self.cancelled.append(tag)
        self.live.pop(tag, None)


class FakeChannel:
    def __init__(self, events, queues):
        self.events = events
        self.queues = queues
        self.qos = None
        self.channel = SimpleNamespace(consumers={})  # underlying aiormq channel
        self._channel = SimpleNamespace(channel=self.channel)  # aio-pika UnderlayChannel
        self.is_closed = False

    async def set_qos(self, prefetch_count):
        self.qos = prefetch_count

    async def declare_queue(self, name, **kwargs):
        queue = self.queues[name]
        queue.tracked = True
        queue.live = self.channel.consumers
        return queue

    async def get_queue(self, name, ensure=True):
        # aio-pika returns an instance the robust channel does not track (nothing is restored).
        queue = self.queues[name]
        queue.live = self.channel.consumers
        return queue


class FakeConnection:
    def __init__(self, events, queues):
        self.events = events
        self.is_closed = False
        self.connected = asyncio.Event()
        self.connected.set()
        self.channels = []
        self.queues = queues
        self.reconnect_callbacks = set()

    async def channel(self, **kwargs):
        channel = FakeChannel(self.events, self.queues)
        channel.kwargs = kwargs
        self.channels.append(channel)
        return channel

    async def close(self):
        self.events.append("connection_closed")
        self.is_closed = True


@pytest.fixture
def started(h, monkeypatch):
    queues = {spec.name: FakeQueue(spec.name) for spec in QUEUES}
    connection = FakeConnection(h.events, queues)
    seen = {}

    async def connect_robust(url, timeout):
        seen["url"] = url
        return connection

    monkeypatch.setattr(consumer_module.aio_pika, "connect_robust", connect_robust)
    return SimpleNamespace(h=h, queues=queues, connection=connection, seen=seen)


@pytest.mark.asyncio
async def test_start_declares_the_topology_sets_prefetch_and_consumes_both_queues(started):
    consumer = started.h.consumer
    assert consumer.is_healthy is False  # not connected yet

    await consumer.start()

    consume_channel, publish_channel = started.connection.channels
    assert consume_channel.qos == 7
    assert publish_channel.kwargs == {"publisher_confirms": True, "on_return_raises": True}
    assert "declare_topology" in started.h.events
    assert set(started.queues) == {QUEUE_SALES, QUEUE_NOTIFICATIONS}
    assert all(hasattr(queue, "callback") for queue in started.queues.values())
    assert consumer.is_healthy is True
    status = consumer.status()
    assert status["status"] == "ok" and status["consuming"] is True
    assert status["counters"] == {"consumed": 0, "retried": 0, "deadLettered": 0, "failed": 0}
    assert "s3cret" not in str(status)


@pytest.mark.asyncio
async def test_health_turns_unhealthy_while_the_connection_is_down_or_closed(started):
    consumer = started.h.consumer
    await consumer.start()

    started.connection.connected.clear()  # robust connection reconnecting
    assert consumer.is_healthy is False and consumer.status()["status"] == "unavailable"
    started.connection.connected.set()
    assert consumer.is_healthy is True
    started.connection.is_closed = True
    assert consumer.is_healthy is False


@pytest.mark.asyncio
async def test_start_consumes_from_queues_tracked_by_the_robust_channel(started):
    """Only tracked queues are re-declared and re-consumed after a broker restart."""
    await started.h.consumer.start()
    assert all(queue.tracked for queue in started.queues.values())


@pytest.mark.asyncio
async def test_health_is_unavailable_while_the_consumers_are_not_registered(started):
    consumer = started.h.consumer
    await consumer.start()
    live = started.connection.channels[0].channel.consumers
    saved = dict(live)

    live.clear()  # connection restored but consumers not re-registered yet
    assert consumer.is_healthy is False
    status = consumer.status()
    assert status["status"] == "unavailable" and status["consuming"] is False

    tag, callback = next(iter(saved.items()))
    live[tag] = callback  # only one of the two queues is back
    assert consumer.is_healthy is False

    live.update(saved)
    assert consumer.is_healthy is True and consumer.status()["consuming"] is True


@pytest.mark.asyncio
async def test_health_is_unavailable_when_the_broker_cancels_one_consumer(started):
    consumer = started.h.consumer
    await consumer.start()
    started.connection.channels[0].channel.consumers.pop(f"tag-{QUEUE_SALES}")
    assert consumer.is_healthy is False


@pytest.mark.asyncio
async def test_health_is_unavailable_while_the_channel_is_being_reopened(started):
    consumer = started.h.consumer
    await consumer.start()

    started.connection.channels[0]._channel = None  # not initialized yet
    assert consumer.is_healthy is False

    class Broken:
        @property
        def channel(self):
            raise RuntimeError("channel is not initialized")

    started.connection.channels[0]._channel = Broken()
    assert consumer.is_healthy is False


@pytest.mark.asyncio
async def test_health_ignores_the_stale_consumers_of_a_closed_channel(started):
    consumer = started.h.consumer
    await consumer.start()
    started.connection.channels[0].is_closed = True  # connection back, channel not restored yet
    assert consumer.is_healthy is False
    started.connection.channels[0].is_closed = False
    assert consumer.is_healthy is True


@pytest.mark.asyncio
async def test_a_reconnect_is_logged(started, caplog):
    await started.h.consumer.start()
    (callback,) = started.connection.reconnect_callbacks
    with caplog.at_level(logging.INFO):
        callback(started.connection)
    assert "event=broker_reconnected" in caplog.text


@pytest.mark.asyncio
async def test_a_failed_start_closes_the_connection_and_reraises(started, monkeypatch):
    async def broken_declare_queue(name, **kwargs):
        raise AMQPConnectionError("down")

    consumer = started.h.consumer
    original = started.connection.channel

    async def channel(**kwargs):
        ch = await original(**kwargs)
        ch.declare_queue = broken_declare_queue
        return ch

    started.connection.channel = channel
    with pytest.raises(AMQPConnectionError):
        await consumer.start()
    assert started.connection.is_closed is True and consumer.is_healthy is False


@pytest.mark.asyncio
async def test_a_start_retried_after_a_partial_subscription_becomes_healthy(started):
    consumer = started.h.consumer
    original = started.connection.channel
    calls = {"declare_queue": 0}

    async def channel(**kwargs):
        ch = await original(**kwargs)
        plain_declare_queue = ch.declare_queue

        async def flaky_declare_queue(name, **kwargs):
            calls["declare_queue"] += 1
            if calls["declare_queue"] == 2:  # first queue subscribed, second one fails
                raise AMQPConnectionError("down")
            return await plain_declare_queue(name, **kwargs)

        ch.declare_queue = flaky_declare_queue
        return ch

    started.connection.channel = channel
    with pytest.raises(AMQPConnectionError):
        await consumer.start()

    started.connection.is_closed = False  # the robust connection comes back
    await consumer.start()
    assert consumer.is_healthy is True


@pytest.mark.asyncio
async def test_stop_cancels_consumers_waits_for_the_in_flight_message_then_closes(started):
    h = started.h
    await h.consumer.start()
    release = asyncio.Event()

    async def slow(*args, **kwargs):
        await release.wait()

    h.handler.side_effect = slow
    in_flight = asyncio.create_task(h.consumer._on_message(SALES, h.message()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert h.consumer.status()["inFlight"] == 1

    stopping = asyncio.create_task(h.consumer.stop())
    await asyncio.sleep(0.01)
    assert all(queue.cancelled for queue in started.queues.values())
    assert "connection_closed" not in h.events  # still waiting for the message
    assert h.consumer.is_healthy is False  # stopping: the healthcheck goes red first

    release.set()
    await in_flight
    await stopping
    assert h.events[1:4] == ["handle", "commit", "ack"]  # after declare_topology
    assert h.events.index("ack") < h.events.index("connection_closed")
    assert started.connection.is_closed is True


@pytest.mark.asyncio
async def test_stop_gives_up_after_the_grace_period(started):
    h = started.h
    h.consumer._shutdown_grace = 0.01
    await h.consumer.start()
    async def forever(*args, **kwargs):
        await asyncio.sleep(10)

    h.handler.side_effect = forever
    task = asyncio.create_task(h.consumer._on_message(SALES, h.message()))
    await asyncio.sleep(0.01)

    await h.consumer.stop()

    assert started.connection.is_closed is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
