"""RabbitMQ consumer of the outbox events (ADR-0030), run as a process separate from the API.

One robust connection and two channels: one consumes with manual ack and `prefetch`, the other
publishes with publisher confirms (retry and dead-letter republications).

Per message: open a DB session, run the shared handler, commit, then ack. On failure:
- retryable (anything except `ModuleRequestError` 400/409 and `NonRetryableTaskError`):
  publish to `<queue>.retry.<wait>` with `x-attempt + 1`, wait for the confirm, then ack the
  original. Once the waits of the queue are exhausted the message is dead-lettered instead.
- not retryable or exhausted: `marcar_fallido` + commit, publish to the DLX, then ack.
- if the DB write or a republication fails, the message is NOT acked: it is requeued
  (nack), so it is redelivered and no event is lost.
`marcar_reintento` is never used: retries live in the broker queues, not in `next_attempt_at`.

Logs carry ids, types and exception class names only; the broker URL never leaves this module.
"""
import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from functools import partial
from typing import Any

import aio_pika
from aio_pika import DeliveryMode, Message
from aio_pika.abc import (
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractRobustChannel,
    AbstractRobustConnection,
)
from aio_pika.exceptions import CONNECTION_EXCEPTIONS, PublishError

from src.shared.domain.module_errors import ModuleRequestError
from src.shared.infrastructure.broker.declare import declare_topology
from src.shared.infrastructure.broker.topology import DEAD_LETTER_EXCHANGE, QUEUES, QueueSpec
from src.shared.infrastructure.persistence.outbox_postgres_adapter import OutboxPostgresAdapter
from src.shared.infrastructure.worker.outbox_handler import NonRetryableTaskError, OutboxTaskHandler
from src.shared.infrastructure.worker.outbox_loop import describe_error

logger = logging.getLogger(__name__)

_BROKER_ERRORS = (*CONNECTION_EXCEPTIONS, asyncio.TimeoutError)
PERMANENT_PREFIX = "PERMANENT_ERROR: "
EXHAUSTED_PREFIX = "RETRIES_EXHAUSTED: "
_MAX_REASON = 300

SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]
HandlerFactory = Callable[[Any], OutboxTaskHandler]


@dataclass
class ConsumerStats:
    """In-process counters, exposed by the healthcheck."""

    consumed: int = 0
    retried: int = 0
    dead_lettered: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "consumed": self.consumed,
            "retried": self.retried,
            "deadLettered": self.dead_lettered,
            "failed": self.failed,
        }


def is_retryable(exc: BaseException) -> bool:
    """Same rule as the direct worker: only 400 and 409 are permanent (SPEC-15)."""
    if isinstance(exc, NonRetryableTaskError):
        return False
    return not (isinstance(exc, ModuleRequestError) and exc.status in (400, 409))


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, ModuleRequestError | NonRetryableTaskError):
        text = str(exc)
    else:
        # Driver and HTTP error messages could echo credentials: class names only.
        text = describe_error(exc)
    return text[:_MAX_REASON]


def _log(level: int, event: str, **fields: Any) -> None:
    pairs = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    logger.log(level, "event=%s %s", event, pairs)


def _attempt_of(message: AbstractIncomingMessage) -> int:
    value = (message.headers or {}).get("x-attempt", 1)
    return value if isinstance(value, int) and value >= 1 else 1


def _job_id_of(message: AbstractIncomingMessage) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(message.message_id))
    except ValueError:
        return None


def _parse(message: AbstractIncomingMessage) -> tuple[str, dict[str, Any]]:
    outbox_type = (message.headers or {}).get("type")
    if not isinstance(outbox_type, str) or not outbox_type:
        raise NonRetryableTaskError("message has no `type` header")
    try:
        payload = json.loads(message.body)
    except ValueError:
        raise NonRetryableTaskError("message body is not valid JSON") from None
    if not isinstance(payload, dict):
        raise NonRetryableTaskError("message body is not a JSON object")
    return outbox_type, payload


class OutboxConsumer:
    def __init__(
        self,
        url: str,
        *,
        prefetch: int,
        session_factory: SessionFactory,
        handler_factory: HandlerFactory,
        connect_timeout: float = 10.0,
        publish_timeout: float = 10.0,
        requeue_delay: float = 1.0,
        shutdown_grace: float = 30.0,
    ):
        self._url = url
        self._prefetch = prefetch
        self._session_factory = session_factory
        self._handler_factory = handler_factory
        self._connect_timeout = connect_timeout
        self._publish_timeout = publish_timeout
        self._requeue_delay = requeue_delay
        self._shutdown_grace = shutdown_grace
        self.stats = ConsumerStats()
        self._connection: AbstractRobustConnection | None = None
        self._consume_channel: AbstractRobustChannel | None = None
        self._publish_channel: AbstractRobustChannel | None = None
        self._subscriptions: list[tuple[Any, str]] = []  # (queue, consumer tag)
        self._in_flight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._stopping = False

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Connects, declares the whole topology and subscribes to both queues."""
        try:
            self._connection = await aio_pika.connect_robust(self._url, timeout=self._connect_timeout)
            self._connection.reconnect_callbacks.add(self._on_reconnected)
            self._consume_channel = await self._connection.channel()
            await self._consume_channel.set_qos(prefetch_count=self._prefetch)
            self._publish_channel = await self._connection.channel(
                publisher_confirms=True, on_return_raises=True
            )
            await declare_topology(self._consume_channel)
            for spec in QUEUES:
                # `declare_queue` (idempotent, same arguments as the topology) returns a queue the
                # robust channel tracks, so after a reconnect it re-declares the queue, its
                # bindings and re-registers this consumer. `get_queue(ensure=False)` returns an
                # untracked instance: nothing would be restored and the consumer would stay silent.
                queue = await self._consume_channel.declare_queue(spec.name, durable=True)
                tag = await queue.consume(partial(self._on_message, spec), no_ack=False)
                self._subscriptions.append((queue, tag))
        except BaseException:
            await self._close_connection()
            raise
        _log(logging.INFO, "consumer_started", queues=",".join(s.name for s in QUEUES), prefetch=self._prefetch)

    def _on_reconnected(self, *_args: Any) -> None:
        # The robust channels restore queues and consumers right after this; /health stays
        # `unavailable` until they are registered again.
        _log(logging.INFO, "broker_reconnected", queues=",".join(s.name for s in QUEUES))

    async def stop(self) -> None:
        """Stops consuming, lets the in-flight messages finish, then closes the connection."""
        self._stopping = True
        for queue, tag in self._subscriptions:
            with contextlib.suppress(Exception):
                await queue.cancel(tag)
        self._subscriptions.clear()
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=self._shutdown_grace)
        except asyncio.TimeoutError:
            _log(logging.WARNING, "shutdown_grace_exceeded", in_flight=self._in_flight)
        await self._close_connection()
        _log(logging.INFO, "consumer_stopped", **self.stats.as_dict())

    async def _close_connection(self) -> None:
        # Subscriptions die with the connection; a retried start() must begin from zero.
        self._subscriptions.clear()
        connection, self._connection = self._connection, None
        self._consume_channel = None
        self._publish_channel = None
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()

    # -- health ----------------------------------------------------------------

    def _live_consumer_count(self) -> int:
        """Consumers the broker channel currently has registered (not the local bookkeeping).

        The underlying channel drops a consumer when it closes or the broker cancels it, and the
        robust channel registers it again once the reconnect completes.
        """
        channel = self._consume_channel
        if channel is None:
            return 0
        try:
            if channel.is_closed:  # still the pre-reconnect channel: its consumers are stale
                return 0
            # `channel.channel` is deprecated and `get_underlay_channel()` is async; the property
            # must stay synchronous for the healthcheck, hence the private attribute.
            underlay = channel._channel  # type: ignore[attr-defined]
            if underlay is None:
                return 0
            live = underlay.channel.consumers
        except Exception:  # channel closed or being reopened by the robust connection
            return 0
        return sum(1 for _queue, tag in self._subscriptions if tag in live)

    @property
    def is_healthy(self) -> bool:
        """True only while the connection is open and every queue has a live consumer."""
        connection = self._connection
        if connection is None or self._stopping:
            return False
        connected = getattr(connection, "connected", None)
        return (
            not connection.is_closed
            and (connected is None or connected.is_set())
            and self._live_consumer_count() == len(QUEUES)
        )

    def status(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.is_healthy else "unavailable",
            "connected": self._connection is not None and not self._connection.is_closed,
            "consuming": self._live_consumer_count() == len(QUEUES),
            "queues": [spec.name for spec in QUEUES],
            "inFlight": self._in_flight,
            "counters": self.stats.as_dict(),
        }

    # -- message handling ------------------------------------------------------

    async def _on_message(self, spec: QueueSpec, message: AbstractIncomingMessage) -> None:
        self._in_flight += 1
        self._idle.clear()
        try:
            await self._process(spec, message)
        finally:
            self._in_flight -= 1
            if self._in_flight == 0:
                self._idle.set()

    async def _process(self, spec: QueueSpec, message: AbstractIncomingMessage) -> None:
        attempt = _attempt_of(message)
        job_id = _job_id_of(message)
        outbox_type = (message.headers or {}).get("type")
        try:
            outbox_type, payload = _parse(message)
            async with self._session_factory() as session:
                handler = self._handler_factory(session)
                await handler.dispatch(job_id or uuid.UUID(int=0), outbox_type, payload, relayed=True)
                await session.commit()
        except Exception as exc:
            await self._on_failure(spec, message, attempt, job_id, outbox_type, exc)
            return
        if await self._settle(message, "ack"):
            self.stats.consumed += 1
            _log(
                logging.INFO, "consumed", queue=spec.name, type=outbox_type,
                message_id=job_id, attempt=attempt,
            )

    async def _on_failure(
        self,
        spec: QueueSpec,
        message: AbstractIncomingMessage,
        attempt: int,
        job_id: uuid.UUID | None,
        outbox_type: Any,
        exc: Exception,
    ) -> None:
        reason = _failure_reason(exc)
        retryable = is_retryable(exc)
        if retryable and attempt <= len(spec.retry_delays):
            delay = spec.retry_delays[attempt - 1]
            try:
                await self._publish(
                    "", spec.retry_queue(delay), message, {"x-attempt": attempt + 1}
                )
            except Exception as publish_exc:
                await self._requeue(message, "retry_publish_failed", spec, job_id, publish_exc)
                return
            if await self._settle(message, "ack"):
                self.stats.retried += 1
                _log(
                    logging.WARNING, "retried", queue=spec.name, type=outbox_type, message_id=job_id,
                    attempt=attempt, next_attempt=attempt + 1, wait_s=int(delay.total_seconds()),
                    reason=reason,
                )
            return
        prefix = PERMANENT_PREFIX if not retryable else EXHAUSTED_PREFIX
        await self._dead_letter(spec, message, attempt, job_id, outbox_type, prefix + reason)

    async def _dead_letter(
        self,
        spec: QueueSpec,
        message: AbstractIncomingMessage,
        attempt: int,
        job_id: uuid.UUID | None,
        outbox_type: Any,
        last_error: str,
    ) -> None:
        if job_id is not None:
            try:
                async with self._session_factory() as session:
                    await OutboxPostgresAdapter(session).marcar_fallido(job_id, last_error)
                    await session.commit()
            except Exception as exc:
                # The row could not be written: keep the message so it is delivered again.
                await self._requeue(message, "mark_failed_failed", spec, job_id, exc)
                return
        else:
            _log(logging.WARNING, "dead_letter_without_row", queue=spec.name, reason="invalid message_id")
        try:
            await self._publish(
                DEAD_LETTER_EXCHANGE,
                spec.dead_letter_queue,
                message,
                {
                    "x-attempt": attempt,
                    "x-failure-reason": last_error,
                    "x-source-queue": spec.name,
                },
            )
        except Exception as exc:
            await self._requeue(message, "dead_letter_publish_failed", spec, job_id, exc)
            return
        if await self._settle(message, "ack"):
            self.stats.dead_lettered += 1
            self.stats.failed += 1
            _log(
                logging.ERROR, "dead_lettered", queue=spec.name, type=outbox_type,
                message_id=job_id, attempt=attempt, reason=last_error,
            )

    async def _requeue(
        self, message: AbstractIncomingMessage, event: str, spec: QueueSpec,
        job_id: uuid.UUID | None, exc: BaseException,
    ) -> None:
        """No ack: give the message back to the broker after a short pause (no hot loop)."""
        _log(logging.ERROR, event, queue=spec.name, message_id=job_id, error=describe_error(exc))
        await asyncio.sleep(self._requeue_delay)
        await self._settle(message, "nack")

    @staticmethod
    async def _settle(message: AbstractIncomingMessage, how: str) -> bool:
        """Acks or requeues; a lost channel only means a redelivery, which is idempotent."""
        try:
            if how == "ack":
                await message.ack()
            else:
                await message.nack(requeue=True)
        except Exception as exc:
            _log(logging.WARNING, "settle_failed", how=how, error=type(exc).__name__)
            return False
        return True

    # -- publishing ------------------------------------------------------------

    async def _publish(
        self, exchange_name: str, routing_key: str, original: AbstractIncomingMessage,
        headers: dict[str, Any],
    ) -> None:
        """Confirmed, mandatory republication of `original` with fresh headers."""
        channel = self._publish_channel
        if channel is None:
            raise ConnectionError("publish channel is closed")
        message = Message(
            body=original.body,
            content_type=original.content_type,
            content_encoding=original.content_encoding,
            delivery_mode=DeliveryMode.PERSISTENT,
            message_id=original.message_id,
            correlation_id=original.correlation_id,
            headers={"type": (original.headers or {}).get("type"), **headers},
        )
        exchange = await self._exchange(channel, exchange_name)
        try:
            await exchange.publish(
                message, routing_key, mandatory=True, timeout=self._publish_timeout
            )
        except PublishError:
            # Unroutable: queues may have been deleted while idle. Re-declare once and retry.
            await declare_topology(channel)
            exchange = await self._exchange(channel, exchange_name)
            await exchange.publish(
                message, routing_key, mandatory=True, timeout=self._publish_timeout
            )

    @staticmethod
    async def _exchange(channel: AbstractRobustChannel, name: str) -> AbstractExchange:
        if not name:
            return channel.default_exchange
        return await channel.get_exchange(name, ensure=False)
