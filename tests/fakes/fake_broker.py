"""Hand fakes of the aio-pika objects the outbox consumer touches (no broker needed)."""
import json
from dataclasses import dataclass, field
from typing import Any

from aio_pika import Message


class FakeIncomingMessage:
    """Records `ack` / `nack` into a shared `events` list so tests can assert ordering."""

    def __init__(
        self,
        outbox_type: str | None = "NOTIFY_SALES_PAYMENT",
        payload: Any = None,
        *,
        message_id: str | None = "5b1c3a52-0d6e-4f64-9a41-0c8f0d3a7e11",
        attempt: int | None = 1,
        body: bytes | None = None,
        events: list | None = None,
    ):
        self.body = (
            body
            if body is not None
            else json.dumps({"order_id": "PED-1"} if payload is None else payload).encode()
        )
        self.message_id = message_id
        self.correlation_id = "corr-1"
        self.content_type = "application/json"
        self.content_encoding = "utf-8"
        headers: dict[str, Any] = {}
        if outbox_type is not None:
            headers["type"] = outbox_type
        if attempt is not None:
            headers["x-attempt"] = attempt
        self.headers = headers
        self.events = events if events is not None else []
        self.ack_error: Exception | None = None

    async def ack(self) -> None:
        if self.ack_error:
            raise self.ack_error
        self.events.append("ack")

    async def nack(self, requeue: bool = True) -> None:
        self.events.append(f"nack(requeue={requeue})")


@dataclass
class Published:
    exchange: str
    routing_key: str
    message: Message
    mandatory: bool

    @property
    def attempt(self) -> int:
        return self.message.headers["x-attempt"]


class FakeExchange:
    def __init__(self, name: str, channel: "FakePublishChannel"):
        self.name = name
        self._channel = channel

    async def publish(self, message, routing_key, *, mandatory=False, timeout=None):
        self._channel.events.append(f"publish:{self.name or 'default'}:{routing_key}")
        if self._channel.errors:
            raise self._channel.errors.pop(0)
        self._channel.published.append(Published(self.name, routing_key, message, mandatory))


@dataclass
class FakePublishChannel:
    events: list = field(default_factory=list)
    published: list[Published] = field(default_factory=list)
    errors: list[Exception] = field(default_factory=list)

    @property
    def default_exchange(self) -> FakeExchange:
        return FakeExchange("", self)

    async def get_exchange(self, name, ensure=True) -> FakeExchange:
        return FakeExchange(name, self)


def unroutable_error(routing_key: str = "x") -> Exception:
    """The `PublishError` aio-pika raises for a returned `mandatory` message."""
    from types import SimpleNamespace

    from aio_pika.exceptions import PublishError
    from pamqp.commands import Basic

    returned = Basic.Return(
        reply_code=312, reply_text="NO_ROUTE", exchange="", routing_key=routing_key
    )
    return PublishError(SimpleNamespace(delivery=returned), returned)  # type: ignore[arg-type]
