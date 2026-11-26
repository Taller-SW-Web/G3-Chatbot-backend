"""Idempotent declaration of the ADR-0030 topology on an aio-pika channel.

Shared by the relay and the future consumer: both declare at startup so the entities
are recreated if the broker deleted idle queues.
"""
from aio_pika import ExchangeType
from aio_pika.abc import AbstractChannel

from src.shared.infrastructure.broker.topology import (
    DEAD_LETTER_EXCHANGE,
    EVENTS_EXCHANGE,
    QUEUES,
)


async def declare_topology(channel: AbstractChannel) -> None:
    events = await channel.declare_exchange(EVENTS_EXCHANGE, ExchangeType.TOPIC, durable=True)
    dead_letter = await channel.declare_exchange(
        DEAD_LETTER_EXCHANGE, ExchangeType.DIRECT, durable=True
    )
    for spec in QUEUES:
        queue = await channel.declare_queue(spec.name, durable=True)
        await queue.bind(events, routing_key=spec.binding_key)

        dlq = await channel.declare_queue(spec.dead_letter_queue, durable=True)
        await dlq.bind(dead_letter, routing_key=spec.dead_letter_queue)

        for delay in spec.retry_delays:
            # Waiting queue without consumer: the TTL expires and the broker returns the
            # message to the origin queue through the default exchange.
            await channel.declare_queue(
                spec.retry_queue(delay),
                durable=True,
                arguments={
                    "x-message-ttl": int(delay.total_seconds() * 1000),
                    "x-dead-letter-exchange": "",
                    "x-dead-letter-routing-key": spec.name,
                },
            )
