"""Outbox worker (SPEC-15, SPEC-16, ADR-0011).

Dispatches transactional outbox events:
- NOTIFY_SALES_PAYMENT: notifies Ventas of approved payment (SPEC-15 Req. 2).
- REQUEST_CANCELLATION: requests cancellation of unpaid/expired orders (SPEC-15 Req. 3).
- SEND_EMAIL: order confirmation email (SPEC-16).

Backoff schedule: 5s, 15s, 45s, 2min, 5min (max 5 attempts).
Permanent failures (400 Bad Request, 409 Conflict) are immediately marked FAILED.

Transactions: each job is fetched alone (`FOR UPDATE SKIP LOCKED`, limit 1),
processed and committed, so PROCESSED / retry / FAILED states persist and the
row lock is held only while that job runs. Two instances never take the same job.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from src.adapters.outbound.persistence.models.outbox import Outbox
from src.adapters.outbound.persistence.order_ref_postgres_adapter import OrderRefPostgresAdapter
from src.adapters.outbound.persistence.outbox_postgres_adapter import OutboxPostgresAdapter
from src.ports.outbound.module_errors import ModuleRequestError
from src.ports.outbound.order_repository_port import SalesOrderPort
from src.ports.outbound.unit_of_work_port import UnitOfWorkPort

logger = logging.getLogger(__name__)

BACKOFF_DELAYS = [
    timedelta(seconds=5),
    timedelta(seconds=15),
    timedelta(seconds=45),
    timedelta(minutes=2),
    timedelta(minutes=5),
]


# A SEND_EMAIL job younger than this is still owned by the inline send of the request
# that enqueued it (SMTP timeout is 10 s); the worker only picks it up afterwards.
INLINE_EMAIL_GRACE = timedelta(seconds=30)


class OutboxProcessor:
    def __init__(
        self,
        outbox_repo: OutboxPostgresAdapter,
        order_ref_repo: OrderRefPostgresAdapter,
        sales_client: SalesOrderPort,
        notification_service: Any | None = None,
        uow: UnitOfWorkPort | None = None,
        email_grace: timedelta | None = None,
    ):
        self.outbox_repo = outbox_repo
        self.order_ref_repo = order_ref_repo
        self.sales_client = sales_client
        self.notification_service = notification_service
        self.uow = uow
        self.email_grace = email_grace

    async def process_one(self, job: Outbox) -> bool:
        """Processes a single outbox record. Returns True if processed, False if retry scheduled/failed."""
        try:
            if job.type == "NOTIFY_SALES_PAYMENT":
                await self._process_notify_sales_payment(job.payload)
                await self.outbox_repo.marcar_procesado(job.id)
                return True
            elif job.type == "REQUEST_CANCELLATION":
                await self._process_request_cancellation(job.payload)
                await self.outbox_repo.marcar_procesado(job.id)
                return True
            elif job.type == "SEND_EMAIL":
                if self.notification_service:
                    from src.ports.outbound.notification_repository_port import EmailTask
                    task = EmailTask(id=job.id, payload=job.payload)
                    await self.notification_service.handle_send_email_task(task)
                    return True
                logger.info("SEND_EMAIL dispatched for payload: %s", job.payload)
                await self.outbox_repo.marcar_procesado(job.id)
                return True
            else:
                logger.warning("Unknown outbox type %s, marking processed", job.type)
                await self.outbox_repo.marcar_procesado(job.id)
                return True

        except ModuleRequestError as exc:
            # 400 or 409 are permanent/inconsistent errors (SPEC-15 Req. 2 & 3).
            # Do NOT retry indefinitely; mark failed immediately for manual review.
            if exc.status in (400, 409):
                logger.error(
                    "Permanent failure (%s - %s) on outbox job %s: %s",
                    exc.status, exc.code, job.id, exc
                )
                await self._mark_permanent_failure(job, str(exc))
                return False
            return await self._handle_retry(job, str(exc))

        except Exception as exc:
            logger.exception("Transient failure on outbox job %s: %s", job.id, exc)
            return await self._handle_retry(job, str(exc))

    async def process_pending_batch(self, limit: int = 20) -> int:
        """Processes up to `limit` due jobs, one transaction per job. Returns the number processed.

        An unexpected error (for example a lost DB connection) rolls the current job back,
        stops the batch and propagates, so the caller can log it and try again later.
        """
        processed = 0
        for _ in range(limit):
            try:
                if self.email_grace:
                    jobs = await self.outbox_repo.listar_pendientes(limite=1, email_gracia=self.email_grace)
                else:
                    jobs = await self.outbox_repo.listar_pendientes(limite=1)
                if not jobs:
                    break
                if await self.process_one(jobs[0]):
                    processed += 1
                await self._commit()
            except Exception:
                await self._rollback()
                raise
        return processed

    async def _commit(self) -> None:
        if self.uow is not None:
            await self.uow.commit()

    async def _rollback(self) -> None:
        if self.uow is not None:
            await self.uow.rollback()

    async def _process_notify_sales_payment(self, payload: dict[str, Any]) -> None:
        order_id = str(payload["order_id"])
        payment_data = payload.get("payment_data", {})
        correlation_id = payload.get("correlation_id")

        await self.sales_client.notify_payment(
            order_id,
            payment_data,
            correlation_id=correlation_id,
        )
        await self.order_ref_repo.actualizar_estado(order_id, "PAID_NOTIFIED")

    async def _process_request_cancellation(self, payload: dict[str, Any]) -> None:
        order_id = str(payload["order_id"])
        motivo = payload.get("motivo", "PAGO_NO_COMPLETADO")
        comentario = payload.get("comentario", "Tiempo límite de espera agotado; cancelado por el canal")
        correlation_id = payload.get("correlation_id")

        await self.sales_client.cancel_order(
            order_id,
            motivo=motivo,
            comentario=comentario,
            correlation_id=correlation_id,
        )
        await self.order_ref_repo.actualizar_estado(order_id, "CANCELLED")

    async def _handle_retry(self, job: Outbox, error_msg: str) -> bool:
        attempt = job.attempts
        delay = BACKOFF_DELAYS[attempt] if attempt < len(BACKOFF_DELAYS) else BACKOFF_DELAYS[-1]
        next_attempt = datetime.now(timezone.utc) + delay
        await self.outbox_repo.marcar_reintento(job.id, next_attempt, last_error=error_msg)
        return False

    async def _mark_permanent_failure(self, job: Outbox, error_msg: str) -> None:
        job.attempts = 5
        job.status = "FAILED"
        job.last_error = f"PERMANENT_ERROR: {error_msg}"
        job.last_attempt_at = datetime.now(timezone.utc)
        job.updated_at = datetime.now(timezone.utc)
        await self.outbox_repo.session.flush()
