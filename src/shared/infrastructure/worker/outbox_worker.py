"""Outbox worker (SPEC-15, SPEC-16, ADR-0011).

Dispatches transactional outbox events:
- NOTIFY_SALES_PAYMENT: notifies Ventas of approved payment (SPEC-15 Req. 2).
- REQUEST_CANCELLATION: requests cancellation of unpaid/expired orders (SPEC-15 Req. 3).
- SEND_EMAIL: order confirmation email (SPEC-16).

Backoff schedule: 5s, 15s, 45s, 2min, 5min (max 5 attempts).
Permanent failures (400 Bad Request, 409 Conflict) are immediately marked FAILED.

Broker mode (ADR-0030, `OUTBOX_DISPATCH_MODE=broker`): the processor is a relay. Each pending
row is published to RabbitMQ with publisher confirms and marked PROCESSED only after the
broker confirmed. If the broker does not confirm, the row stays PENDING and is rescheduled
(5 s to 60 s) WITHOUT incrementing `attempts`, so a long outage never drives rows to FAILED.

Transactions: each job is fetched alone (`FOR UPDATE SKIP LOCKED`, limit 1),
processed and committed, so PROCESSED / retry / FAILED states persist and the
row lock is held only while that job runs. Two instances never take the same job.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from src.shared.application.ports.order_repository_port import SalesOrderPort
from src.shared.application.ports.outbox_publisher_port import (
    BrokerUnavailableError,
    OutboxMessage,
    OutboxPublisherPort,
    UnknownOutboxTypeError,
)
from src.shared.application.unit_of_work_port import UnitOfWorkPort
from src.shared.domain.module_errors import ModuleRequestError
from src.shared.infrastructure.db.models.outbox import Outbox
from src.shared.infrastructure.persistence.order_ref_postgres_adapter import OrderRefPostgresAdapter
from src.shared.infrastructure.persistence.outbox_postgres_adapter import OutboxPostgresAdapter
from src.shared.infrastructure.worker.outbox_handler import HandlerUnavailableError, OutboxTaskHandler

logger = logging.getLogger(__name__)

BACKOFF_DELAYS = [
    timedelta(seconds=5),
    timedelta(seconds=15),
    timedelta(seconds=45),
    timedelta(minutes=2),
    timedelta(minutes=5),
]


# Broker-down reschedule (ADR-0030): doubles from the previous delay, bounded to 5 s .. 60 s.
BROKER_BACKOFF_MIN = timedelta(seconds=5)
BROKER_BACKOFF_MAX = timedelta(seconds=60)


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
        feedback_notifier: Any | None = None,
        publisher: OutboxPublisherPort | None = None,
    ):
        self.outbox_repo = outbox_repo
        self.order_ref_repo = order_ref_repo
        self.sales_client = sales_client
        self.notification_service = notification_service
        self.uow = uow
        self.email_grace = email_grace
        self.feedback_notifier = feedback_notifier
        # Set only in broker mode; None keeps the direct dispatch below untouched.
        self.publisher = publisher
        self._broker_unavailable = False
        # The per-type dispatch is shared with the broker consumer (ADR-0030).
        self.handler = OutboxTaskHandler(
            outbox_repo, order_ref_repo, sales_client, notification_service, feedback_notifier
        )

    async def process_one(self, job: Outbox) -> bool:
        """Processes a single outbox record. Returns True if processed, False if retry scheduled/failed."""
        try:
            if self.publisher is not None:
                return await self._relay_to_broker(job, self.publisher)
            await self.handler.dispatch(job.id, job.type, job.payload)
            return True

        except HandlerUnavailableError as exc:
            return await self._handle_retry(job, str(exc))

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
        self._broker_unavailable = False
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
                if self._broker_unavailable:
                    # The broker is down: leave the remaining rows PENDING for the next poll.
                    break
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

    async def _relay_to_broker(self, job: Outbox, publisher: OutboxPublisherPort) -> bool:
        # `attempts` counts completed retries; the broker header is 1-based. A row that
        # already retried in direct mode must keep its attempt number after the switch.
        message = OutboxMessage(
            id=job.id, type=job.type, payload=job.payload, attempt=job.attempts + 1
        )
        try:
            await publisher.publish(message)
        except BrokerUnavailableError as exc:
            self._broker_unavailable = True
            delay = self._broker_delay(job)
            logger.warning(
                "Broker did not confirm outbox job %s, rescheduled in %ds: %s",
                job.id, int(delay.total_seconds()), exc,
            )
            await self.outbox_repo.reprogramar(
                job.id, datetime.now(timezone.utc) + delay, last_error=str(exc)
            )
            return False
        except UnknownOutboxTypeError as exc:
            logger.error("Outbox job %s cannot be routed: %s", job.id, exc)
            await self._mark_permanent_failure(job, str(exc))
            return False
        await self.outbox_repo.marcar_procesado(job.id)
        return True

    @staticmethod
    def _broker_delay(job: Outbox) -> timedelta:
        """Doubles the previous reschedule delay (`next_attempt_at - last_attempt_at`)."""
        if job.last_attempt_at is None or job.next_attempt_at is None:
            return BROKER_BACKOFF_MIN
        previous = job.next_attempt_at - job.last_attempt_at
        return min(BROKER_BACKOFF_MAX, max(BROKER_BACKOFF_MIN, previous * 2))

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
