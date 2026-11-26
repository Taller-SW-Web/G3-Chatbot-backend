"""aio-pika adapter of `OutboxPublisherPort` (ADR-0030).

One long-lived robust connection and one channel with publisher confirms and
`on_return_raises`, so an unroutable (`mandatory`) message, a nack, a timeout or a lost
connection all surface as the single `BrokerUnavailableError`. Only exception class
names are logged or stored: the URL and its credentials never leave this module.
"""
import asyncio
import contextlib
import json
import logging

import aio_pika
from aio_pika import DeliveryMode, Message
from aio_pika.abc import AbstractExchange, AbstractRobustChannel, AbstractRobustConnection
from aio_pika.exceptions import CONNECTION_EXCEPTIONS

from src.shared.application.ports.outbox_publisher_port import (
    BrokerUnavailableError,
    OutboxMessage,
)
from src.shared.infrastructure.broker.declare import declare_topology
from src.shared.infrastructure.broker.topology import EVENTS_EXCHANGE, routing_key_for

logger = logging.getLogger(__name__)

_BROKER_ERRORS = (*CONNECTION_EXCEPTIONS, asyncio.TimeoutError)


class RabbitMqOutboxPublisher:
    def __init__(self, url: str, *, connect_timeout: float = 10.0, publish_timeout: float = 10.0):
        self._url = url
        self._connect_timeout = connect_timeout
        self._publish_timeout = publish_timeout
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractRobustChannel | None = None
        self._exchange: AbstractExchange | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Opens the connection and declares the topology; a broker that is down is not fatal.

        Rows stay PENDING and the first publish retries the connection, so the API can
        boot while the broker is unreachable.
        """
        try:
            await self._ensure_ready()
        except BrokerUnavailableError as exc:
            logger.warning("RabbitMQ unavailable at startup, will retry on demand: %s", exc)
        else:
            logger.info("RabbitMQ relay connected and topology declared")

    async def close(self) -> None:
        async with self._lock:
            await self._close_quietly()

    async def publish(self, message: OutboxMessage) -> None:
        routing_key = routing_key_for(message.type)  # fails before any I/O
        exchange = await self._ensure_ready()
        try:
            await exchange.publish(
                self._build_message(message),
                routing_key,
                mandatory=True,
                timeout=self._publish_timeout,
            )
        except _BROKER_ERRORS as exc:
            raise BrokerUnavailableError(
                f"publish not confirmed by the broker ({type(exc).__name__})"
            ) from None

    @staticmethod
    def _build_message(message: OutboxMessage) -> Message:
        correlation_id = message.payload.get("correlation_id")
        return Message(
            body=json.dumps(message.payload, ensure_ascii=False, separators=(",", ":")).encode(),
            content_type="application/json",
            content_encoding="utf-8",
            delivery_mode=DeliveryMode.PERSISTENT,
            message_id=str(message.id),
            correlation_id=str(correlation_id) if correlation_id else None,
            headers={"type": message.type, "x-attempt": message.attempt},
        )

    async def _ensure_ready(self) -> AbstractExchange:
        async with self._lock:
            # A robust connection restores itself after a drop; reopen only when none exists.
            if self._exchange is not None:
                return self._exchange
            try:
                self._connection = await aio_pika.connect_robust(
                    self._url, timeout=self._connect_timeout
                )
                self._channel = await self._connection.channel(
                    publisher_confirms=True, on_return_raises=True
                )
                await declare_topology(self._channel)
                self._exchange = await self._channel.get_exchange(EVENTS_EXCHANGE, ensure=False)
            except _BROKER_ERRORS as exc:
                await self._close_quietly()
                raise BrokerUnavailableError(
                    f"cannot connect to the broker ({type(exc).__name__})"
                ) from None
            return self._exchange

    async def _close_quietly(self) -> None:
        connection, self._connection = self._connection, None
        self._channel = None
        self._exchange = None
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()
