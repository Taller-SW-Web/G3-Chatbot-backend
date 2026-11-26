"""Outbox relay against a real RabbitMQ (ADR-0030). Skipped without RABBITMQ_TEST_URL.

Run a local broker with `docker run -d --rm -p 5672:5672 rabbitmq:4` and set
RABBITMQ_TEST_URL=amqp://guest:guest@localhost:5672/ . Never point it at CloudAMQP.
"""
import json
import os
import uuid

import aio_pika
import pytest
import pytest_asyncio

from src.shared.application.ports.outbox_publisher_port import BrokerUnavailableError, OutboxMessage
from src.shared.infrastructure.broker.rabbitmq_outbox_publisher import RabbitMqOutboxPublisher
from src.shared.infrastructure.broker.topology import QUEUE_NOTIFICATIONS, QUEUE_SALES, QUEUES

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def rabbitmq_url():
    url = os.getenv("RABBITMQ_TEST_URL")
    if not url:
        pytest.skip("Falta RABBITMQ_TEST_URL: integración RabbitMQ no ejecutada")
    return url


@pytest_asyncio.fixture
async def publisher(rabbitmq_url):
    relay = RabbitMqOutboxPublisher(rabbitmq_url, connect_timeout=5, publish_timeout=5)
    await relay.start()
    yield relay
    await relay.close()


@pytest_asyncio.fixture
async def channel(rabbitmq_url, publisher):
    connection = await aio_pika.connect_robust(rabbitmq_url)
    ch = await connection.channel()
    for queue in (QUEUE_SALES, QUEUE_NOTIFICATIONS):
        await (await ch.get_queue(queue)).purge()
    yield ch
    for queue in (QUEUE_SALES, QUEUE_NOTIFICATIONS):
        await (await ch.get_queue(queue)).purge()
    await connection.close()


async def test_publishes_a_persistent_confirmed_message_that_reaches_its_queue(publisher, channel):
    message = OutboxMessage(
        id=uuid.uuid4(),
        type="NOTIFY_SALES_PAYMENT",
        payload={"order_id": "PED-IT-1", "correlation_id": "corr-it-1"},
    )

    await publisher.publish(message)

    incoming = await (await channel.get_queue(QUEUE_SALES)).get(timeout=5)
    async with incoming.process():
        assert json.loads(incoming.body) == message.payload
        assert incoming.message_id == str(message.id)
        assert incoming.correlation_id == "corr-it-1"
        assert incoming.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
        assert incoming.content_type == "application/json"
        assert incoming.routing_key == "ventas.pago.notificar"
        assert incoming.headers["type"] == "NOTIFY_SALES_PAYMENT"
        assert incoming.headers["x-attempt"] == 1


@pytest.mark.parametrize(
    ("outbox_type", "queue"),
    [
        ("REQUEST_CANCELLATION", QUEUE_SALES),
        ("SEND_EMAIL", QUEUE_NOTIFICATIONS),
        ("SEND_FEEDBACK_EMAIL", QUEUE_NOTIFICATIONS),
    ],
)
async def test_every_outbox_type_is_routed_to_its_queue(publisher, channel, outbox_type, queue):
    message = OutboxMessage(id=uuid.uuid4(), type=outbox_type, payload={"order_id": "PED-IT-2"})
    await publisher.publish(message)

    incoming = await (await channel.get_queue(queue)).get(timeout=5)
    async with incoming.process():
        assert incoming.message_id == str(message.id)


async def test_topology_is_declared_and_idempotent(rabbitmq_url, publisher, channel):
    # A second relay re-declares the same entities without a precondition failure.
    second = RabbitMqOutboxPublisher(rabbitmq_url, connect_timeout=5)
    await second.start()
    await second.publish(OutboxMessage(id=uuid.uuid4(), type="SEND_EMAIL", payload={"order_id": "x"}))
    await second.close()

    for spec in QUEUES:
        await channel.get_queue(spec.dead_letter_queue)  # passive declare: raises if missing
        for delay in spec.retry_delays:
            await channel.get_queue(spec.retry_queue(delay))


async def test_an_unreachable_broker_is_reported_as_broker_unavailable():
    relay = RabbitMqOutboxPublisher("amqp://guest:guest@127.0.0.1:1/", connect_timeout=2)
    await relay.start()  # tolerated: the rows stay PENDING
    with pytest.raises(BrokerUnavailableError):
        await relay.publish(
            OutboxMessage(id=uuid.uuid4(), type="SEND_EMAIL", payload={"order_id": "x"})
        )
