"""In-memory `OutboxPublisherPort` for tests: records what would reach the broker."""
from dataclasses import dataclass
from typing import Any

from src.shared.application.ports.outbox_publisher_port import (
    BrokerUnavailableError,
    OutboxMessage,
)
from src.shared.infrastructure.broker.topology import routing_key_for


@dataclass(frozen=True)
class PublishedMessage:
    routing_key: str
    message: OutboxMessage

    @property
    def payload(self) -> dict[str, Any]:
        return self.message.payload


class FakeOutboxPublisher:
    def __init__(self, *, available: bool = True):
        self.available = available
        self.published: list[PublishedMessage] = []

    async def publish(self, message: OutboxMessage) -> None:
        routing_key = routing_key_for(message.type)
        if not self.available:
            raise BrokerUnavailableError("fake broker is down")
        self.published.append(PublishedMessage(routing_key, message))
