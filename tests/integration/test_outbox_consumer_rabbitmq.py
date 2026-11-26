"""Relay -> RabbitMQ -> consumer end to end (ADR-0030).

Needs RABBITMQ_TEST_URL (skipped otherwise); the PostgreSQL part also needs TEST_DATABASE_URL.
Run a local broker with `docker run -d --rm -p 5672:5672 rabbitmq:4` and set
RABBITMQ_TEST_URL=amqp://guest:guest@localhost:5672/ . Never point it at CloudAMQP.
"""
import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aio_pika
import pytest
import pytest_asyncio

from src.modules.mod_orders.adapters.fake_email_sender import FakeEmailSender
from src.shared.application.ports.outbox_publisher_port import OutboxMessage
from src.shared.domain.module_errors import ModuleRequestError, ModuleUnavailableError
from src.shared.infrastructure.broker import consumer as consumer_module
from src.shared.infrastructure.broker.consumer import OutboxConsumer
from src.shared.infrastructure.broker.rabbitmq_outbox_publisher import RabbitMqOutboxPublisher
from src.shared.infrastructure.broker.topology import QUEUE_NOTIFICATIONS, QUEUE_SALES, QUEUES
from src.shared.infrastructure.db.models import Outbox
from src.shared.infrastructure.persistence.order_ref_postgres_adapter import OrderRefPostgresAdapter
from src.shared.infrastructure.persistence.outbox_postgres_adapter import OutboxPostgresAdapter
from src.shared.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.shared.infrastructure.worker.outbox_handler import OutboxTaskHandler
from src.shared.infrastructure.worker.outbox_worker import OutboxProcessor
from tests.integration.mod_orders.test_notification_orders_postgres import (
    ORDER_ID,
    _paid_order,
    _service,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ALL_QUEUES = [
    name
    for spec in QUEUES
    for name in (spec.name, spec.dead_letter_queue, *(spec.retry_queue(d) for d in spec.retry_delays))
]


async def eventually(predicate, timeout: float = 15.0):
    """Polls a sync or async predicate until it returns a truthy value."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = predicate()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("condition not met in time")
        await asyncio.sleep(0.05)


@pytest.fixture
def rabbitmq_url():
    url = os.getenv("RABBITMQ_TEST_URL")
    if not url:
        pytest.skip("Falta RABBITMQ_TEST_URL: integración RabbitMQ no ejecutada")
    return url


@pytest_asyncio.fixture
async def relay(rabbitmq_url):
    publisher = RabbitMqOutboxPublisher(rabbitmq_url, connect_timeout=5, publish_timeout=5)
    await publisher.start()
    yield publisher
    await publisher.close()


@pytest_asyncio.fixture
async def inspector(rabbitmq_url, relay):
    """Second connection that clears every queue around a test and reads queue depths."""
    connection = await aio_pika.connect_robust(rabbitmq_url)
    channel = await connection.channel()

    async def purge():
        for name in ALL_QUEUES:
            await (await channel.get_queue(name)).purge()

    async def depth(name: str) -> int:
        # A fresh channel per read: aio-pika caches queue objects (and their old counts).
        async with connection.channel() as fresh:
            return (await fresh.get_queue(name)).declaration_result.message_count

    await purge()
    yield SimpleNamespace(channel=channel, depth=depth)
    await purge()
    await connection.close()


class Recorder:
    """What the handler saw, plus a programmable failure per call."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.errors: list[Exception] = []

    async def dispatch(self, job_id, job_type, payload, **kwargs):
        self.calls.append((str(job_id), job_type, payload, kwargs))
        if self.errors:
            raise self.errors.pop(0)


@asynccontextmanager
async def running_consumer(url, session_factory, handler_factory, *, prefetch=5):
    consumer = OutboxConsumer(
        url,
        prefetch=prefetch,
        session_factory=session_factory,
        handler_factory=handler_factory,
        requeue_delay=0.1,
        shutdown_grace=5,
    )
    await consumer.start()
    try:
        yield consumer
    finally:
        await consumer.stop()


@asynccontextmanager
async def _null_session():
    yield SimpleNamespace(commit=AsyncMock())


def _message(outbox_type="NOTIFY_SALES_PAYMENT", payload=None) -> OutboxMessage:
    return OutboxMessage(id=uuid.uuid4(), type=outbox_type, payload=payload or {"order_id": "PED-IT-1"})


# -- broker only (handler faked) --------------------------------------------------------


async def test_a_published_message_is_consumed_in_another_connection_and_acked(
    rabbitmq_url, relay, inspector
):
    recorder = Recorder()
    message = _message(payload={"order_id": "PED-IT-1", "correlation_id": "corr-9"})
    async with running_consumer(rabbitmq_url, _null_session, lambda s: recorder) as consumer:
        assert consumer.is_healthy
        await relay.publish(message)
        await eventually(lambda: consumer.stats.consumed == 1)

    (job_id, job_type, payload, kwargs) = recorder.calls[0]
    assert (job_id, job_type) == (str(message.id), "NOTIFY_SALES_PAYMENT")
    assert payload == message.payload and kwargs == {"relayed": True}
    assert await inspector.depth(QUEUE_SALES) == 0  # acked, nothing left unacked either
    assert not consumer.is_healthy  # stopped


async def test_a_transient_failure_waits_in_the_retry_queue_and_is_redelivered(
    rabbitmq_url, relay, inspector
):
    recorder = Recorder()
    recorder.errors.append(ModuleUnavailableError("ventas", "timeout"))
    async with running_consumer(rabbitmq_url, _null_session, lambda s: recorder) as consumer:
        await relay.publish(_message())
        await eventually(lambda: consumer.stats.retried == 1)
        assert await inspector.depth("chatbot.ventas.retry.5s") == 1
        # After the 5 s TTL the broker returns it to chatbot.ventas and the second pass succeeds.
        await eventually(lambda: consumer.stats.consumed == 1, timeout=15)

    assert len(recorder.calls) == 2
    assert await inspector.depth("chatbot.ventas.retry.5s") == 0


async def test_a_permanent_error_marks_the_row_failed_and_lands_in_the_dlq(
    rabbitmq_url, relay, inspector, monkeypatch
):
    marked: list[tuple[uuid.UUID, str]] = []

    async def marcar_fallido(job_id, last_error):
        marked.append((job_id, last_error))

    monkeypatch.setattr(
        consumer_module, "OutboxPostgresAdapter", lambda s: SimpleNamespace(marcar_fallido=marcar_fallido)
    )
    recorder = Recorder()
    recorder.errors.append(ModuleRequestError("ventas", 409, "CONFLICTO", "already cancelled"))
    message = _message("REQUEST_CANCELLATION")
    async with running_consumer(rabbitmq_url, _null_session, lambda s: recorder) as consumer:
        await relay.publish(message)
        await eventually(lambda: consumer.stats.dead_lettered == 1)

    assert marked[0][0] == message.id and marked[0][1].startswith("PERMANENT_ERROR: ")
    dead = await (await inspector.channel.get_queue("chatbot.ventas.dlq")).get(timeout=5)
    async with dead.process():
        assert dead.message_id == str(message.id)
        assert dead.headers["x-attempt"] == 1
        assert "409" in dead.headers["x-failure-reason"]
    assert await inspector.depth(QUEUE_SALES) == 0


async def test_an_unacked_message_is_redelivered_when_the_consumer_goes_away(
    rabbitmq_url, relay, inspector
):
    """Manual ack: stopping mid-message without finishing leaves it in the queue."""
    started = asyncio.Event()

    class Hang:
        async def dispatch(self, *args, **kwargs):
            started.set()
            await asyncio.sleep(60)

    consumer = OutboxConsumer(
        rabbitmq_url, prefetch=1, session_factory=_null_session, handler_factory=lambda s: Hang(),
        shutdown_grace=0.1,
    )
    await consumer.start()
    await relay.publish(_message())
    await asyncio.wait_for(started.wait(), 10)
    await consumer.stop()  # grace expires, connection closes with the message unacked

    recorder = Recorder()
    async with running_consumer(rabbitmq_url, _null_session, lambda s: recorder) as second:
        await eventually(lambda: second.stats.consumed == 1)
    assert len(recorder.calls) == 1


async def test_the_topology_is_recreated_when_the_consumer_starts(rabbitmq_url, relay, inspector):
    await inspector.channel.get_queue(QUEUE_NOTIFICATIONS)  # exists
    channel = inspector.channel
    for spec in QUEUES:  # simulate CloudAMQP deleting idle queues
        for name in (spec.dead_letter_queue, *(spec.retry_queue(d) for d in spec.retry_delays)):
            await (await channel.get_queue(name)).delete()

    async with running_consumer(rabbitmq_url, _null_session, lambda s: Recorder()):
        for name in ALL_QUEUES:
            await channel.get_queue(name)  # passive declare: raises if missing


# -- relay -> consumer with the real handlers on PostgreSQL ------------------------------


def _real_handler_factory(session, sales, email_sender):
    def build(_session):
        return OutboxTaskHandler(
            OutboxPostgresAdapter(session),
            OrderRefPostgresAdapter(session),
            sales,
            notification_service=_service(session, email_sender),
        )

    return build


def _shared_session(session):
    lock = asyncio.Lock()  # one test transaction: messages of both queues take turns

    @asynccontextmanager
    async def factory():
        async with lock:
            yield session  # the fixture rolls the test transaction back

    return factory


async def _enqueue(session, outbox_type, payload) -> Outbox:
    return await OutboxPostgresAdapter(session).guardar(Outbox(type=outbox_type, payload=payload))


async def _relay_pending(session, publisher):
    processor = OutboxProcessor(
        OutboxPostgresAdapter(session),
        OrderRefPostgresAdapter(session),
        AsyncMock(),
        uow=SqlAlchemyUnitOfWork(session),
        publisher=publisher,
    )
    return await processor.process_pending_batch()


async def _reload(session, row):
    await session.refresh(row)
    return row


async def test_a_payment_and_a_confirmation_email_flow_from_the_outbox_through_the_broker(
    rabbitmq_url, relay, inspector, postgres_session
):
    session = postgres_session
    await _paid_order(session)
    await _service(session, FakeEmailSender()).enqueue_confirmation(ORDER_ID)  # needs PAID_NOTIFIED
    await OrderRefPostgresAdapter(session).actualizar_estado(ORDER_ID, "PAYMENT_PENDING_NOTIFICATION")
    payment = await _enqueue(
        session,
        "NOTIFY_SALES_PAYMENT",
        {"order_id": ORDER_ID, "payment_data": {"transaccionId": "TX-IT-1"}, "correlation_id": "c-1"},
    )
    sales, email_sender = AsyncMock(), FakeEmailSender()

    assert await _relay_pending(session, relay) == 2  # relay: both rows PROCESSED after the confirm
    assert (await _reload(session, payment)).status == "PROCESSED"
    processed_at = payment.processed_at

    factory = _real_handler_factory(session, sales, email_sender)
    async with running_consumer(rabbitmq_url, _shared_session(session), factory, prefetch=1) as consumer:
        await eventually(lambda: consumer.stats.consumed == 2)

        sales.notify_payment.assert_awaited_once_with(
            ORDER_ID, {"transaccionId": "TX-IT-1"}, correlation_id="c-1"
        )
        order = await OrderRefPostgresAdapter(session).obtener_por_order_id(ORDER_ID)
        assert order.local_status == "PAID_NOTIFIED"
        assert len(email_sender.sent) == 1

        # Redelivery of both messages (at-least-once): no second effect, no retry, no DLQ.
        for row in await _all_rows(session):
            await relay.publish(OutboxMessage(id=row.id, type=row.type, payload=row.payload))
        await eventually(lambda: consumer.stats.consumed == 4)

        sales.notify_payment.assert_awaited_once()
        assert len(email_sender.sent) == 1
        assert consumer.stats.retried == 0 and consumer.stats.dead_lettered == 0

    # The consumer never re-marks a relayed row: processed_at is still the broker-confirm time.
    assert (await _reload(session, payment)).processed_at == processed_at
    assert payment.attempts == 0


async def _all_rows(session):
    from sqlalchemy import select

    return list((await session.execute(select(Outbox))).scalars().all())


async def test_a_ventas_conflict_ends_failed_in_the_database_and_in_the_dlq(
    rabbitmq_url, relay, inspector, postgres_session
):
    session = postgres_session
    await _paid_order(session)
    cancellation = await _enqueue(session, "REQUEST_CANCELLATION", {"order_id": ORDER_ID})
    sales = AsyncMock()
    sales.cancel_order.side_effect = ModuleRequestError("ventas", 409, "CONFLICTO", "already cancelled")

    assert await _relay_pending(session, relay) == 1
    factory = _real_handler_factory(session, sales, FakeEmailSender())
    async with running_consumer(rabbitmq_url, _shared_session(session), factory, prefetch=1) as consumer:
        await eventually(lambda: consumer.stats.dead_lettered == 1)

    row = await _reload(session, cancellation)
    assert row.status == "FAILED" and row.last_error.startswith("PERMANENT_ERROR: ")
    assert row.attempts == 0  # `marcar_reintento` is never used by the consumer
    assert await inspector.depth("chatbot.ventas.dlq") == 1
    assert sales.cancel_order.await_count == 1
