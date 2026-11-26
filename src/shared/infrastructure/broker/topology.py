"""RabbitMQ topology and routing keys for the outbox (ADR-0030).

Pure data, no broker client import, so the relay (publisher) and the future consumer
(H5-M3) declare and route with the same names.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from src.shared.application.ports.outbox_publisher_port import UnknownOutboxTypeError

EVENTS_EXCHANGE = "chatbot.eventos"
DEAD_LETTER_EXCHANGE = "chatbot.eventos.dlx"

QUEUE_SALES = "chatbot.ventas"
QUEUE_NOTIFICATIONS = "chatbot.notificaciones"

# Outbox type -> routing key (ADR-0030, "Claves de enrutamiento por tipo de tarea").
ROUTING_KEYS: Mapping[str, str] = {
    "NOTIFY_SALES_PAYMENT": "ventas.pago.notificar",
    "REQUEST_CANCELLATION": "ventas.anulacion.solicitar",
    "SEND_EMAIL": "notificaciones.correo.confirmacion",
    "SEND_FEEDBACK_EMAIL": "notificaciones.correo.retroalimentacion",
}


@dataclass(frozen=True)
class QueueSpec:
    name: str
    binding_key: str
    retry_delays: tuple[timedelta, ...]

    @property
    def dead_letter_queue(self) -> str:
        return f"{self.name}.dlq"

    def retry_queue(self, delay: timedelta) -> str:
        return retry_queue_name(self.name, delay)


QUEUES: tuple[QueueSpec, ...] = (
    QueueSpec(
        name=QUEUE_SALES,
        binding_key="ventas.#",
        retry_delays=(
            timedelta(seconds=5),
            timedelta(seconds=15),
            timedelta(seconds=45),
            timedelta(minutes=2),
            timedelta(minutes=5),
        ),
    ),
    QueueSpec(
        name=QUEUE_NOTIFICATIONS,
        binding_key="notificaciones.#",
        # Same waits as the e-mail policy of SPEC-16.
        retry_delays=(timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15)),
    ),
)


def routing_key_for(outbox_type: str) -> str:
    try:
        return ROUTING_KEYS[outbox_type]
    except KeyError:
        raise UnknownOutboxTypeError(f"no routing key for outbox type {outbox_type!r}") from None


def queue_for(outbox_type: str) -> str:
    """Queue that receives the type, derived from its routing key and the queue bindings."""
    prefix = routing_key_for(outbox_type).split(".", 1)[0]
    for spec in QUEUES:
        if spec.binding_key == f"{prefix}.#":
            return spec.name
    raise UnknownOutboxTypeError(f"no queue bound for outbox type {outbox_type!r}")


def retry_queue_name(queue: str, delay: timedelta) -> str:
    """`<queue>.retry.<wait>`, for example `chatbot.ventas.retry.45s` or `.retry.2m`."""
    seconds = int(delay.total_seconds())
    wait = f"{seconds // 60}m" if seconds % 60 == 0 else f"{seconds}s"
    return f"{queue}.retry.{wait}"
