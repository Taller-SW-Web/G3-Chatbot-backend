"""Routing keys and topology of the outbox relay (ADR-0030)."""
from datetime import timedelta

import pytest

from src.shared.application.ports.outbox_publisher_port import UnknownOutboxTypeError
from src.shared.infrastructure.broker.declare import declare_topology
from src.shared.infrastructure.broker.topology import (
    DEAD_LETTER_EXCHANGE,
    EVENTS_EXCHANGE,
    QUEUE_NOTIFICATIONS,
    QUEUE_SALES,
    QUEUES,
    queue_for,
    retry_queue_name,
    routing_key_for,
)

ADR_ROUTING = {
    "NOTIFY_SALES_PAYMENT": ("ventas.pago.notificar", QUEUE_SALES),
    "REQUEST_CANCELLATION": ("ventas.anulacion.solicitar", QUEUE_SALES),
    "SEND_EMAIL": ("notificaciones.correo.confirmacion", QUEUE_NOTIFICATIONS),
    "SEND_FEEDBACK_EMAIL": ("notificaciones.correo.retroalimentacion", QUEUE_NOTIFICATIONS),
}


@pytest.mark.parametrize("outbox_type", ADR_ROUTING)
def test_every_outbox_type_has_the_adr_routing_key_and_queue(outbox_type):
    key, queue = ADR_ROUTING[outbox_type]
    assert routing_key_for(outbox_type) == key
    assert queue_for(outbox_type) == queue


def test_unknown_type_has_no_route():
    with pytest.raises(UnknownOutboxTypeError):
        routing_key_for("SOMETHING_ELSE")
    with pytest.raises(UnknownOutboxTypeError):
        queue_for("SOMETHING_ELSE")


def test_every_key_matches_its_queue_binding():
    from fnmatch import fnmatch

    for outbox_type in ADR_ROUTING:
        binding = next(q.binding_key for q in QUEUES if q.name == queue_for(outbox_type))
        assert fnmatch(routing_key_for(outbox_type), binding.replace("#", "*"))


@pytest.mark.parametrize(
    ("delay", "suffix"),
    [
        (timedelta(seconds=5), "5s"),
        (timedelta(seconds=45), "45s"),
        (timedelta(minutes=1), "1m"),
        (timedelta(minutes=2), "2m"),
        (timedelta(minutes=15), "15m"),
    ],
)
def test_retry_queue_names(delay, suffix):
    assert retry_queue_name("chatbot.ventas", delay) == f"chatbot.ventas.retry.{suffix}"


def test_wait_policy_matches_the_adr():
    by_name = {q.name: q.retry_delays for q in QUEUES}
    assert by_name[QUEUE_SALES] == (
        timedelta(seconds=5), timedelta(seconds=15), timedelta(seconds=45),
        timedelta(minutes=2), timedelta(minutes=5),
    )
    assert by_name[QUEUE_NOTIFICATIONS] == (
        timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15),
    )


class _Entity:
    def __init__(self, name, log, **options):
        self.name, self.log, self.options = name, log, options

    async def bind(self, exchange, routing_key):
        self.log["bindings"].append((self.name, exchange.name, routing_key))


class _Channel:
    def __init__(self):
        self.log = {"exchanges": {}, "queues": {}, "bindings": []}

    async def declare_exchange(self, name, type, durable):
        self.log["exchanges"][name] = (type.value, durable)
        return _Entity(name, self.log)

    async def declare_queue(self, name, durable=False, arguments=None):
        self.log["queues"][name] = {"durable": durable, "arguments": arguments}
        return _Entity(name, self.log)


@pytest.mark.asyncio
async def test_declares_the_full_adr_topology():
    channel = _Channel()
    await declare_topology(channel)
    log = channel.log

    assert log["exchanges"] == {EVENTS_EXCHANGE: ("topic", True), DEAD_LETTER_EXCHANGE: ("direct", True)}
    # 2 queues + 2 dead-letter queues + 5 + 3 waiting queues.
    assert len(log["queues"]) == 12
    assert all(q["durable"] for q in log["queues"].values())
    assert set(log["bindings"]) == {
        (QUEUE_SALES, EVENTS_EXCHANGE, "ventas.#"),
        (QUEUE_NOTIFICATIONS, EVENTS_EXCHANGE, "notificaciones.#"),
        ("chatbot.ventas.dlq", DEAD_LETTER_EXCHANGE, "chatbot.ventas.dlq"),
        ("chatbot.notificaciones.dlq", DEAD_LETTER_EXCHANGE, "chatbot.notificaciones.dlq"),
    }
    assert log["queues"]["chatbot.ventas.retry.45s"]["arguments"] == {
        "x-message-ttl": 45_000,
        "x-dead-letter-exchange": "",
        "x-dead-letter-routing-key": QUEUE_SALES,
    }
    assert log["queues"]["chatbot.notificaciones.retry.15m"]["arguments"] == {
        "x-message-ttl": 900_000,
        "x-dead-letter-exchange": "",
        "x-dead-letter-routing-key": QUEUE_NOTIFICATIONS,
    }
