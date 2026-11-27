"""Per-type dispatch of an outbox task, shared by the direct worker and the consumer (ADR-0030).

`OutboxProcessor` (direct mode) and the RabbitMQ consumer run the very same effects:
- NOTIFY_SALES_PAYMENT: call Ventas and follow its `nuevoEstado`: PAGADO -> PAID_NOTIFIED plus
  the confirmation e-mail; ANULADO (late payment) -> CANCELLED, no e-mail.
- REQUEST_CANCELLATION: call Ventas, then update `order_ref`; a 409 PEDIDO_NO_ANULABLE is a
  success when Ventas reports the order as ANULADO.
- SEND_EMAIL: order confirmation through the notification service.
- SEND_FEEDBACK_EMAIL: internal feedback e-mail.

The two callers differ in who owns the outbox row:
- direct (`relayed=False`): the handler closes the row itself (`marcar_procesado`, or the
  notification service for SEND_EMAIL), exactly as the processor always did.
- relayed (`relayed=True`, consumer): the relay already marked the row PROCESSED when the
  broker confirmed it, so the handler never touches the row. Redeliveries are made safe by
  the receivers (Ventas `transaccionId`, unique notification rows) and e-mail outcomes are
  reported by raising.

Failures are raised, never swallowed: the caller decides between retry, FAILED and DLQ.
"""
import logging
import uuid
from typing import Any

from src.shared.application.ports.notification_repository_port import EmailTask
from src.shared.application.ports.order_repository_port import SalesOrderPort
from src.shared.domain.module_errors import ModuleRequestError
from src.shared.infrastructure.persistence.order_ref_postgres_adapter import OrderRefPostgresAdapter
from src.shared.infrastructure.persistence.outbox_postgres_adapter import OutboxPostgresAdapter

logger = logging.getLogger(__name__)

PAID_NOTIFIED = "PAID_NOTIFIED"
CANCELLED = "CANCELLED"
SALES_PAID = "PAGADO"
SALES_CANCELLED = "ANULADO"


class HandlerUnavailableError(Exception):
    """A collaborator the task needs is not wired; retryable once it is."""


class NonRetryableTaskError(Exception):
    """The task can never succeed (bad payload, unknown type, e-mail closed as FAILED)."""


class EmailSendRetryError(Exception):
    """The notification service scheduled an e-mail retry; the consumer retries via its queues."""


class OutboxTaskHandler:
    def __init__(
        self,
        outbox_repo: OutboxPostgresAdapter,
        order_ref_repo: OrderRefPostgresAdapter,
        sales_client: SalesOrderPort,
        notification_service: Any | None = None,
        feedback_notifier: Any | None = None,
    ):
        self.outbox_repo = outbox_repo
        self.order_ref_repo = order_ref_repo
        self.sales_client = sales_client
        self.notification_service = notification_service
        self.feedback_notifier = feedback_notifier

    async def dispatch(
        self, job_id: uuid.UUID, job_type: str, payload: dict[str, Any], *, relayed: bool = False
    ) -> None:
        """Runs the effects of one task. Raises on failure; see the module docstring."""
        if job_type == "NOTIFY_SALES_PAYMENT":
            await self._notify_sales_payment(payload, relayed)
            await self._close_row(job_id, relayed)
        elif job_type == "REQUEST_CANCELLATION":
            await self._request_cancellation(payload, relayed)
            await self._close_row(job_id, relayed)
        elif job_type == "SEND_EMAIL":
            await self._send_email(job_id, payload, relayed)
        elif job_type == "SEND_FEEDBACK_EMAIL":
            if self.feedback_notifier is None:
                logger.error("SEND_FEEDBACK_EMAIL without a notifier; keeping the job PENDING")
                raise HandlerUnavailableError("feedback notifier unavailable")
            await self.feedback_notifier.send(payload)
            await self._close_row(job_id, relayed)
        elif relayed:
            raise NonRetryableTaskError(f"unknown outbox type {job_type!r}")
        else:
            logger.warning("Unknown outbox type %s, marking processed", job_type)
            await self.outbox_repo.marcar_procesado(job_id)

    async def _close_row(self, job_id: uuid.UUID, relayed: bool) -> None:
        if not relayed:
            await self.outbox_repo.marcar_procesado(job_id)

    @staticmethod
    def _order_id(payload: dict[str, Any]) -> str:
        try:
            return str(payload["order_id"])
        except KeyError:
            raise NonRetryableTaskError("payload has no order_id") from None

    # No local-status redelivery guard: `order_ref.local_status` never tells whether Ventas got
    # the call. Redeliveries rely on Ventas: payments deduplicate by `transaccionId` (ADR-0012)
    # and a repeated cancellation answers 409, which is reconciled below. The confirmation
    # e-mail is idempotent by itself (`enqueue_confirmation` registers `(order_id, 0)` once).

    async def _notify_sales_payment(self, payload: dict[str, Any], relayed: bool) -> None:
        order_id = self._order_id(payload)
        payment_data = payload.get("payment_data", {})
        correlation_id = payload.get("correlation_id")

        # Ventas deduplicates by `transaccionId` (ADR-0012), so a repeated call is harmless.
        result = await self.sales_client.notify_payment(
            order_id,
            payment_data,
            correlation_id=correlation_id,
        )
        new_state = result.get("nuevoEstado") if isinstance(result, dict) else None

        if new_state == SALES_CANCELLED:
            # Late payment on an order Ventas already cancelled (for example no stock): the
            # money is refunded by Ventas, so the order is never confirmed to the customer.
            logger.info(
                "Payment notified on a cancelled order: order_id=%s correlation_id=%s nuevoEstado=%s",
                order_id, correlation_id, new_state,
            )
            await self.order_ref_repo.actualizar_estado(order_id, CANCELLED)
            return

        if new_state != SALES_PAID:
            # `nuevoEstado` is optional in the contract. A 200 means Ventas applied an approved
            # payment, so the previous behavior (paid) is kept, but it is worth a warning.
            logger.warning(
                "Ventas answered the payment without a PAGADO/ANULADO state: order_id=%s "
                "correlation_id=%s nuevoEstado=%r; treating the order as paid",
                order_id, correlation_id, new_state,
            )
        await self.order_ref_repo.actualizar_estado(order_id, PAID_NOTIFIED)
        await self._enqueue_confirmation(order_id)

    async def _enqueue_confirmation(self, order_id: str) -> None:
        service = self.notification_service
        if service is None:
            logger.error("Order %s is paid but no notification service is wired; no confirmation e-mail", order_id)
            return
        # Idempotent: a redelivered task finds the registered notification and does nothing.
        await service.enqueue_confirmation(order_id)

    async def _request_cancellation(self, payload: dict[str, Any], relayed: bool) -> None:
        order_id = self._order_id(payload)
        motivo = payload.get("motivo", "PAGO_NO_COMPLETADO")
        comentario = payload.get("comentario", "Tiempo límite de espera agotado; cancelado por el canal")
        correlation_id = payload.get("correlation_id")

        try:
            await self.sales_client.cancel_order(
                order_id,
                motivo=motivo,
                comentario=comentario,
                correlation_id=correlation_id,
            )
        except ModuleRequestError as exc:
            if exc.status != 409 or exc.code != "PEDIDO_NO_ANULABLE":
                raise
            # SOLICITUD_DUPLICADA is left as a permanent failure: it means a request is still
            # pending authorization, not that the order is cancelled.
            try:
                order = await self.sales_client.get_order_as_service(order_id, correlation_id=correlation_id)
            except ModuleRequestError as read_exc:
                # The read was refused (scope, unknown order): the original 409 stands.
                logger.warning(
                    "Could not confirm the order state after a 409: order_id=%s status=%s code=%s",
                    order_id, read_exc.status, read_exc.code,
                )
                raise exc from read_exc
            if not isinstance(order, dict) or order.get("estado") != SALES_CANCELLED:
                raise
            logger.info(
                "Cancellation already applied in Ventas: order_id=%s correlation_id=%s",
                order_id, correlation_id,
            )
        await self.order_ref_repo.actualizar_estado(order_id, CANCELLED)

    async def _send_email(self, job_id: uuid.UUID, payload: dict[str, Any], relayed: bool) -> None:
        service = self.notification_service
        if not relayed:
            if service:
                # The service closes the outbox row itself (PROCESSED, retry or FAILED).
                await service.handle_send_email_task(EmailTask(id=job_id, payload=payload))
                return
            logger.info("SEND_EMAIL dispatched for payload: %s", payload)
            await self.outbox_repo.marcar_procesado(job_id)
            return
        if service is None:
            raise HandlerUnavailableError("notification service unavailable")
        # `send_confirmation` is the single send step: it skips SENT rows (unique
        # `(order_id, resend_number)`) and keeps its own attempt counter in `notification`.
        # Not `handle_send_email_task`: that one would mark the outbox row a second time.
        result = await service.send_confirmation(
            str(payload.get("order_id", "")), int(payload.get("resend_number", 0))
        )
        outcome = getattr(result.outcome, "value", result.outcome)
        if outcome == "RETRY":
            raise EmailSendRetryError(result.error or "e-mail send failed, retry scheduled")
        if outcome == "FAILED":
            raise NonRetryableTaskError(result.error or "e-mail send failed")
