"""Order confirmation e-mail: enqueue, send, retry and resend (SPEC-16).

Entry points:
- `enqueue_confirmation(order_id)`: after the order reaches `PAID_NOTIFIED`
  (SPEC-15), registers the confirmation (`resend_number` 0) and its
  `SEND_EMAIL` outbox task in one transaction. Idempotent per order.
- `send_confirmation(order_id, resend_number=0)`: the single send step.
  Never sends twice (`SENT` rows are skipped, unique `(order_id,
  resend_number)`). Returns the outcome; it never raises for provider errors.
- `handle_send_email_task(task)`: what the outbox worker runs for each
  `SEND_EMAIL` task. Schedules the retries at 1, 5 and 15 min and closes the
  task; after the third failed retry the notification stays `FAILED`.
- `resend_confirmation(...)`: the "Reenviar" action (max. 2 per order,
  `CONFIRMATION_RESEND`), deduplicated by customer, order and Idempotency-Key.

With `send_inline` the task is also run right after enqueueing it, so the
e-mail goes out before the outbox worker exists; the worker later finds it
`SENT` and only closes the task. A failure here never reaches the purchase
flow (SPEC-16 Req. 3). Never imports FastAPI, SQLAlchemy, smtplib or Jinja2.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from src.application.notifications.order_confirmation import (
    build_confirmation_context,
    chat_notice,
    recipient_of,
)
from src.application.use_cases.manage_session_use_case import mask_email
from src.ports.outbound.email_sender_port import (
    EmailDeliveryError,
    EmailMessage,
    EmailSenderPort,
    EmailTemplatePort,
)
from src.ports.outbound.notification_repository_port import (
    ConfirmationSourcePort,
    EmailOutboxPort,
    EmailTask,
    NotificationRepositoryPort,
)
from src.ports.outbound.unit_of_work_port import UnitOfWorkPort

logger = logging.getLogger(__name__)

MAX_RESENDS = 2
# Retry delays after each failed send; the fourth failure is final (SPEC-16 Req. 2).
RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15))
PAID_STATUS = "PAID_NOTIFIED"


class NotificationError(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(detail or code)


class SendOutcome(str, Enum):
    SENT = "SENT"
    ALREADY_SENT = "ALREADY_SENT"
    RETRY = "RETRY"
    FAILED = "FAILED"


@dataclass(frozen=True)
class SendResult:
    outcome: SendOutcome
    next_attempt_at: datetime | None = None
    error: str | None = None


class NotificationService:
    def __init__(
        self,
        notifications: NotificationRepositoryPort,
        outbox: EmailOutboxPort,
        sources: ConfirmationSourcePort,
        sender: EmailSenderPort,
        templates: EmailTemplatePort,
        uow: UnitOfWorkPort,
        *,
        frontend_base_url: str,
        send_inline: bool = True,
        clock=None,
    ):
        self._notifications = notifications
        self._outbox = outbox
        self._sources = sources
        self._sender = sender
        self._templates = templates
        self._uow = uow
        self._frontend_base_url = frontend_base_url.rstrip("/")
        self._send_inline = send_inline
        self._now = clock or (lambda: datetime.now(timezone.utc))

    # -- enqueue -------------------------------------------------------------

    async def enqueue_confirmation(self, order_id: str) -> dict:
        """Register and enqueue the confirmation of a paid order.

        Returns `{correoEnmascarado, mensaje}` for the `CONFIRMACION_PEDIDO`
        block, or an empty `mensaje` when there is no address to write to.
        """
        source = await self._sources.cargar(order_id)
        if source is None or source.local_status != PAID_STATUS:
            raise NotificationError("RECURSO_NO_ENCONTRADO", f"order {order_id} is not paid")
        recipient = recipient_of(source)
        if recipient is None:
            logger.warning("Order %s has no contact e-mail; confirmation not enqueued", order_id)
            return {"correoEnmascarado": None, "mensaje": None}
        existing = await self._notifications.obtener(order_id, 0)
        if existing is None:
            await self._notifications.crear_si_no_existe(order_id, 0, recipient)
            task = await self._outbox.encolar_envio({"order_id": order_id, "resend_number": 0})
            await self._uow.commit()
            await self._run_inline(task)
        return {"correoEnmascarado": mask_email(recipient), "mensaje": chat_notice(recipient)}

    # -- send ----------------------------------------------------------------

    async def send_confirmation(self, order_id: str, resend_number: int = 0) -> SendResult:
        notification = await self._notifications.obtener(order_id, resend_number)
        if notification is None and resend_number == 0:
            # Direct call without a prior enqueue: register it first.
            source = await self._sources.cargar(order_id)
            recipient = recipient_of(source) if source else None
            if recipient is None:
                return SendResult(SendOutcome.FAILED, error="order or recipient not found")
            notification = await self._notifications.crear_si_no_existe(order_id, 0, recipient)
        if notification is None:
            return SendResult(SendOutcome.FAILED, error="notification not found")
        if notification.status == "SENT":
            return SendResult(SendOutcome.ALREADY_SENT)
        if notification.status == "FAILED":
            return SendResult(SendOutcome.FAILED, error=notification.last_error)

        try:
            message = await self._build_message(order_id, notification.recipient)
        except NotificationError as exc:
            # Missing snapshot data is not transient: no retry.
            await self._notifications.cerrar_fallida(order_id, resend_number, exc.detail)
            await self._uow.commit()
            return SendResult(SendOutcome.FAILED, error=exc.detail)

        try:
            await self._sender.send(message)
        except EmailDeliveryError as exc:
            return await self._register_failure(order_id, resend_number, notification.attempts, str(exc))
        await self._notifications.marcar_enviada(order_id, resend_number)
        await self._uow.commit()
        return SendResult(SendOutcome.SENT)

    async def handle_send_email_task(self, task: EmailTask) -> SendResult:
        order_id = str(task.payload.get("order_id", ""))
        resend_number = int(task.payload.get("resend_number", 0))
        result = await self.send_confirmation(order_id, resend_number)
        if result.outcome is SendOutcome.RETRY:
            # Invariante de `SendResult`: RETRY siempre trae la fecha del proximo intento.
            assert result.next_attempt_at is not None
            await self._outbox.marcar_reintento(task.id, result.next_attempt_at, result.error)
        elif result.outcome is SendOutcome.FAILED:
            await self._outbox.marcar_fallido(task.id, result.error or "send failed")
        else:
            await self._outbox.marcar_procesado(task.id)
        await self._uow.commit()
        return result

    # -- resend --------------------------------------------------------------

    async def resend_confirmation(self, customer_id, order_id: str, *, idempotency_key: str) -> dict:
        source = await self._sources.cargar(order_id)
        if source is None or str(source.customer_id) != str(customer_id) or source.local_status != PAID_STATUS:
            # Foreign, unknown or unpaid orders look the same (SPEC-17 Req. 1).
            raise NotificationError("RECURSO_NO_ENCONTRADO", "No encontré ese pedido en tu cuenta.")
        recipient = recipient_of(source)
        if recipient is None:
            raise NotificationError("RECURSO_NO_ENCONTRADO", "Ese pedido no tiene un correo de contacto.")

        previous = await self._outbox.buscar_envio_por_clave(order_id, str(customer_id), idempotency_key)
        if previous is not None:
            # Same Idempotency-Key: same answer, no new e-mail, no resend consumed.
            return self._resend_response(order_id, int(previous.payload["resend_number"]), recipient)

        sent_resends = await self._notifications.contar_reenvios(order_id)
        if sent_resends >= MAX_RESENDS:
            raise NotificationError(
                "LIMITE_REENVIOS_EXCEDIDO",
                "Ya reenviamos la confirmación 2 veces; revisa tu carpeta de spam.",
            )
        resend_number = sent_resends + 1
        await self._notifications.crear_si_no_existe(order_id, resend_number, recipient)
        task = await self._outbox.encolar_envio({
            "order_id": order_id,
            "resend_number": resend_number,
            "customer_id": str(customer_id),
            "idempotency_key": idempotency_key,
        })
        await self._uow.commit()
        await self._run_inline(task)
        return self._resend_response(order_id, resend_number, recipient)

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _resend_response(order_id: str, resend_number: int, recipient: str) -> dict:
        return {
            "ok": True,
            "pedidoId": order_id,
            "reenviosRestantes": MAX_RESENDS - resend_number,
            "correoEnmascarado": mask_email(recipient),
        }

    async def _run_inline(self, task: EmailTask) -> None:
        if not self._send_inline:
            return
        try:
            await self.handle_send_email_task(task)
        except Exception:
            # The outbox task stays PENDING for the worker; the purchase is not affected.
            logger.exception("Inline send of %s failed", task.payload)

    async def _build_message(self, order_id: str, recipient: str) -> EmailMessage:
        source = await self._sources.cargar(order_id)
        if source is None:
            raise NotificationError("RECURSO_NO_ENCONTRADO", f"order {order_id} snapshot not found")
        context = build_confirmation_context(
            source, order_link=f"{self._frontend_base_url}/pedidos/{order_id}"
        )
        rendered = self._templates.render_order_confirmation(context)
        return EmailMessage(to=recipient, subject=rendered.subject, html=rendered.html, text=rendered.text)

    async def _register_failure(
        self, order_id: str, resend_number: int, attempts: int, error: str
    ) -> SendResult:
        if attempts < len(RETRY_DELAYS):
            await self._notifications.registrar_reintento(order_id, resend_number, error)
            await self._uow.commit()
            return SendResult(SendOutcome.RETRY, next_attempt_at=self._now() + RETRY_DELAYS[attempts], error=error)
        await self._notifications.cerrar_fallida(order_id, resend_number, error)
        await self._uow.commit()
        logger.warning("Confirmation %s/%s FAILED after retries: %s", order_id, resend_number, error)
        return SendResult(SendOutcome.FAILED, error=error)
