"""Outbound ports for the order confirmation e-mail (SPEC-16, docs/modelo-datos.md).

- `NotificationRepositoryPort`: one `notification` row per send, unique on
  `(order_id, resend_number)`; 0 is the confirmation, 1 and 2 the resends.
- `EmailOutboxPort`: `SEND_EMAIL` tasks that the outbox worker (SPEC-15) runs.
- `ConfirmationSourcePort`: the local snapshot the e-mail is built from
  (`order_ref`, the checkout `summary` and the approved payment attempt).
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class NotificationRecord:
    order_id: str
    resend_number: int
    recipient: str
    status: str
    attempts: int
    last_error: str | None = None


@dataclass(frozen=True)
class EmailTask:
    id: uuid.UUID
    payload: dict[str, Any]


@dataclass(frozen=True)
class ConfirmationSource:
    order_id: str
    customer_id: uuid.UUID
    local_status: str
    created_at: datetime
    summary: dict[str, Any] = field(default_factory=dict)
    card_brand: str | None = None
    card_last4: str | None = None


class NotificationRepositoryPort(Protocol):
    async def crear_si_no_existe(
        self, order_id: str, resend_number: int, recipient: str
    ) -> NotificationRecord:
        """Insert the row as `PENDING`, or return the existing one (idempotent)."""
        ...

    async def obtener(self, order_id: str, resend_number: int) -> NotificationRecord | None:
        ...

    async def contar_reenvios(self, order_id: str) -> int:
        ...

    async def marcar_enviada(self, order_id: str, resend_number: int) -> Any:
        ...

    async def registrar_reintento(self, order_id: str, resend_number: int, last_error: str) -> Any:
        """Count one SMTP retry and keep the row `PENDING`."""
        ...

    async def cerrar_fallida(self, order_id: str, resend_number: int, last_error: str) -> Any:
        """Final `FAILED` state, without counting another retry."""
        ...


class EmailOutboxPort(Protocol):
    async def encolar_envio(self, payload: dict[str, Any]) -> EmailTask:
        """Insert a `SEND_EMAIL` task, due now."""
        ...

    async def buscar_envio_por_clave(
        self, order_id: str, customer_id: str, idempotency_key: str
    ) -> EmailTask | None:
        ...

    async def marcar_procesado(self, id: uuid.UUID) -> Any:
        ...

    async def marcar_reintento(
        self, id: uuid.UUID, next_attempt_at: datetime, last_error: str | None = None
    ) -> Any:
        ...

    async def marcar_fallido(self, id: uuid.UUID, last_error: str) -> Any:
        ...


class ConfirmationSourcePort(Protocol):
    async def cargar(self, order_id: str) -> ConfirmationSource | None:
        ...
