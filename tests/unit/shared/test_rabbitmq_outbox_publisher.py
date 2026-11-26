"""aio-pika adapter of the outbox publisher (ADR-0030), with the broker client faked."""
import json
import logging
import uuid

import pytest
from aio_pika import DeliveryMode
from aio_pika.exceptions import (
    CONNECTION_EXCEPTIONS,
    AMQPConnectionError,
    ChannelInvalidStateError,
    DeliveryError,
    PublishError,
)
from pamqp.commands import Basic

from src.shared.application.ports.outbox_publisher_port import (
    BrokerUnavailableError,
    OutboxMessage,
    UnknownOutboxTypeError,
)
from src.shared.infrastructure.broker import rabbitmq_outbox_publisher as adapter
from src.shared.infrastructure.broker.rabbitmq_outbox_publisher import RabbitMqOutboxPublisher

URL = "amqps://svc-user:s3cret-pw@broker.example.com/vh"


class FakeExchange:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def publish(self, message, routing_key, *, mandatory, timeout):
        if self.error:
            raise self.error
        self.calls.append((message, routing_key, mandatory))


class FakeChannel:
    def __init__(self, exchange):
        self.exchange = exchange

    async def get_exchange(self, name, ensure):
        assert name == "chatbot.eventos"
        return self.exchange


class FakeConnection:
    def __init__(self, exchange):
        self.exchange = exchange
        self.channel_kwargs = None
        self.closed = False

    async def channel(self, **kwargs):
        self.channel_kwargs = kwargs
        return FakeChannel(self.exchange)

    async def close(self):
        self.closed = True


@pytest.fixture
def broker(monkeypatch):
    state = {"exchange": FakeExchange(), "connect_error": None, "connections": [], "topology": 0}

    async def connect_robust(url, timeout):
        if state["connect_error"]:
            raise state["connect_error"]
        connection = FakeConnection(state["exchange"])
        state["connections"].append(connection)
        return connection

    async def declare_topology(channel):
        state["topology"] += 1

    monkeypatch.setattr(adapter.aio_pika, "connect_robust", connect_robust)
    monkeypatch.setattr(adapter, "declare_topology", declare_topology)
    return state


def _message(outbox_type="NOTIFY_SALES_PAYMENT", payload=None):
    return OutboxMessage(
        id=uuid.uuid4(), type=outbox_type, payload=payload or {"order_id": "PED-1", "n": "ñ"}
    )


@pytest.mark.asyncio
async def test_publishes_persistent_json_with_the_adr_properties(broker):
    publisher = RabbitMqOutboxPublisher(URL)
    message = _message(payload={"order_id": "PED-1", "correlation_id": "corr-1", "n": "ñ"})

    await publisher.publish(message)

    (amqp, routing_key, mandatory), = broker["exchange"].calls
    assert routing_key == "ventas.pago.notificar"
    assert mandatory is True
    assert json.loads(amqp.body) == message.payload
    assert amqp.content_type == "application/json"
    assert amqp.delivery_mode == DeliveryMode.PERSISTENT
    assert amqp.message_id == str(message.id)
    assert amqp.correlation_id == "corr-1"
    assert amqp.headers == {"type": "NOTIFY_SALES_PAYMENT", "x-attempt": 1}


@pytest.mark.asyncio
async def test_correlation_id_is_omitted_when_the_payload_has_none(broker):
    await RabbitMqOutboxPublisher(URL).publish(_message("SEND_EMAIL"))
    (amqp, routing_key, _), = broker["exchange"].calls
    assert amqp.correlation_id is None
    assert routing_key == "notificaciones.correo.confirmacion"


@pytest.mark.asyncio
async def test_connection_uses_confirms_and_declares_the_topology_once(broker):
    publisher = RabbitMqOutboxPublisher(URL)
    await publisher.publish(_message())
    await publisher.publish(_message("REQUEST_CANCELLATION"))

    assert len(broker["connections"]) == 1
    assert broker["connections"][0].channel_kwargs == {
        "publisher_confirms": True, "on_return_raises": True,
    }
    assert broker["topology"] == 1


@pytest.mark.asyncio
async def test_unknown_type_fails_before_any_io(broker):
    with pytest.raises(UnknownOutboxTypeError):
        await RabbitMqOutboxPublisher(URL).publish(_message("NOPE"))
    assert broker["connections"] == []


def _nack():
    return DeliveryError(None, Basic.Nack(delivery_tag=1))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [_nack(), ChannelInvalidStateError("channel closed"), ConnectionResetError("reset"), TimeoutError()],
    ids=["nack", "channel-closed", "connection-reset", "timeout"],
)
async def test_unconfirmed_publication_is_a_broker_unavailable_error(broker, error):
    broker["exchange"].error = error
    with pytest.raises(BrokerUnavailableError):
        await RabbitMqOutboxPublisher(URL).publish(_message())


def test_unroutable_returns_are_covered_by_the_mapped_errors():
    # `mandatory=True` + `on_return_raises=True` raises PublishError for an unroutable message.
    assert issubclass(PublishError, DeliveryError)
    assert issubclass(PublishError, CONNECTION_EXCEPTIONS)


@pytest.mark.asyncio
async def test_connect_failure_is_broker_unavailable_and_never_leaks_the_url(broker):
    broker["connect_error"] = AMQPConnectionError(f"cannot reach {URL}")
    with pytest.raises(BrokerUnavailableError) as raised:
        await RabbitMqOutboxPublisher(URL).publish(_message())
    assert "s3cret-pw" not in str(raised.value)
    assert "broker.example.com" not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.asyncio
async def test_start_tolerates_a_broker_that_is_down_and_publish_reconnects(broker, caplog):
    broker["connect_error"] = AMQPConnectionError(f"cannot reach {URL}")
    publisher = RabbitMqOutboxPublisher(URL)
    with caplog.at_level(logging.INFO):
        await publisher.start()  # must not raise
    assert "s3cret-pw" not in caplog.text
    assert "unavailable at startup" in caplog.text

    broker["connect_error"] = None
    await publisher.publish(_message())
    assert len(broker["exchange"].calls) == 1


@pytest.mark.asyncio
async def test_start_logs_no_credentials_when_it_connects(broker, caplog):
    with caplog.at_level(logging.INFO):
        await RabbitMqOutboxPublisher(URL).start()
    assert "connected" in caplog.text
    assert "s3cret-pw" not in caplog.text
    assert "broker.example.com" not in caplog.text


@pytest.mark.asyncio
async def test_close_closes_the_connection_and_a_later_publish_reopens_it(broker):
    publisher = RabbitMqOutboxPublisher(URL)
    await publisher.start()
    first = broker["connections"][0]

    await publisher.close()
    assert first.closed is True

    await publisher.publish(_message())
    assert len(broker["connections"]) == 2
