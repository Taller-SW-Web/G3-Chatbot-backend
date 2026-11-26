"""Port that relays an outbox row to the message broker (ADR-0030).

The application layer only knows this protocol and its errors; the aio-pika adapter
lives in `src/shared/infrastructure/broker`.
"""
import uuid
from dataclasses import dataclass
from typing import Any, Protocol


class BrokerUnavailableError(Exception):
    """The broker did not confirm the publication (down, nack, unroutable or lost connection).

    The message never carries the broker URL or credentials: it is stored in `last_error`.
    """


class UnknownOutboxTypeError(ValueError):
    """The outbox type has no routing key, so it can never be published."""


@dataclass(frozen=True)
class OutboxMessage:
    """What the relay publishes: the outbox row identity plus its JSON payload."""

    id: uuid.UUID
    type: str
    payload: dict[str, Any]
    # 1-based delivery attempt, sent as the `x-attempt` header.
    attempt: int = 1


class OutboxPublisherPort(Protocol):
    async def publish(self, message: OutboxMessage) -> None:
        """Returns only after the broker confirmed the message.

        Raises `BrokerUnavailableError` when it is not confirmed and
        `UnknownOutboxTypeError` when the type has no route.
        """
        ...
