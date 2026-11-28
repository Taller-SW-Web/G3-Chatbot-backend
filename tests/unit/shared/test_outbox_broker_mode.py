"""OutboxProcessor in broker mode (ADR-0030): relay with confirms, no attempts burned."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.shared.infrastructure.db.models.outbox import Outbox
from src.shared.infrastructure.persistence.outbox_postgres_adapter import OutboxPostgresAdapter
from src.shared.infrastructure.worker.outbox_worker import (
    BROKER_BACKOFF_MAX,
    BROKER_BACKOFF_MIN,
    OutboxProcessor,
)
from tests.fakes.fake_outbox_publisher import FakeOutboxPublisher


def _job(job_type="NOTIFY_SALES_PAYMENT", payload=None, **fields):
    return Outbox(
        id=uuid.uuid4(),
        type=job_type,
        payload=payload or {"order_id": "PED-B", "correlation_id": "corr-9"},
        status="PENDING",
        attempts=fields.pop("attempts", 0),
        **fields,
    )


@pytest.fixture
def deps():
    return AsyncMock(), AsyncMock(), AsyncMock()  # outbox_repo, order_ref_repo, sales_client


def _processor(deps, publisher, **kwargs):
    outbox_repo, order_ref_repo, sales_client = deps
    return OutboxProcessor(outbox_repo, order_ref_repo, sales_client, publisher=publisher, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job_type", "routing_key"),
    [
        ("NOTIFY_SALES_PAYMENT", "ventas.pago.notificar"),
        ("REQUEST_CANCELLATION", "ventas.anulacion.solicitar"),
        ("SEND_EMAIL", "notificaciones.correo.confirmacion"),
        ("SEND_FEEDBACK_EMAIL", "notificaciones.correo.retroalimentacion"),
    ],
)
async def test_publishes_instead_of_dispatching_and_marks_processed_after_the_confirm(
    deps, job_type, routing_key
):
    outbox_repo, order_ref_repo, sales_client = deps
    publisher = FakeOutboxPublisher()
    events: list[str] = []
    original = publisher.publish

    async def publish(message):
        await original(message)
        events.append("confirmed")

    publisher.publish = publish
    outbox_repo.marcar_procesado.side_effect = lambda *_: events.append("processed")
    job = _job(job_type)

    assert await _processor(deps, publisher).process_one(job) is True

    assert events == ["confirmed", "processed"]
    outbox_repo.marcar_procesado.assert_awaited_once_with(job.id)
    (published,) = publisher.published
    assert published.routing_key == routing_key
    assert published.message.id == job.id
    assert published.message.type == job_type
    assert published.payload == job.payload
    assert published.message.attempt == 1
    # No direct side effects: Ventas and the order reference are not touched by the relay.
    sales_client.notify_payment.assert_not_awaited()
    sales_client.cancel_order.assert_not_awaited()
    order_ref_repo.actualizar_estado.assert_not_awaited()


@pytest.mark.asyncio
async def test_x_attempt_keeps_the_attempts_already_consumed_in_direct_mode(deps):
    publisher = FakeOutboxPublisher()
    job = _job(attempts=2)

    assert await _processor(deps, publisher).process_one(job) is True

    (published,) = publisher.published
    assert published.message.attempt == 3


@pytest.mark.asyncio
async def test_send_email_is_not_delegated_to_the_notification_service_in_broker_mode(deps):
    notification_service = AsyncMock()
    processor = _processor(deps, FakeOutboxPublisher(), notification_service=notification_service)
    await processor.process_one(_job("SEND_EMAIL", {"order_id": "PED-E"}))
    notification_service.handle_send_email_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_broker_down_keeps_the_row_pending_and_does_not_count_an_attempt(deps):
    outbox_repo, *_ = deps
    job = _job(attempts=2)
    before = datetime.now(timezone.utc)

    assert await _processor(deps, FakeOutboxPublisher(available=False)).process_one(job) is False

    outbox_repo.marcar_procesado.assert_not_awaited()
    outbox_repo.marcar_reintento.assert_not_awaited()  # would increment `attempts`
    outbox_repo.reprogramar.assert_awaited_once()
    job_id, next_attempt = outbox_repo.reprogramar.await_args.args
    assert job_id == job.id
    assert before + BROKER_BACKOFF_MIN <= next_attempt <= before + BROKER_BACKOFF_MIN + timedelta(seconds=5)
    assert "fake broker is down" in outbox_repo.reprogramar.await_args.kwargs["last_error"]
    assert job.status == "PENDING"
    assert job.attempts == 2


@pytest.mark.parametrize(
    ("previous", "expected"),
    [
        (None, BROKER_BACKOFF_MIN),
        (timedelta(seconds=5), timedelta(seconds=10)),
        (timedelta(seconds=10), timedelta(seconds=20)),
        (timedelta(seconds=40), BROKER_BACKOFF_MAX),
        (timedelta(minutes=5), BROKER_BACKOFF_MAX),
        (timedelta(seconds=1), BROKER_BACKOFF_MIN),
    ],
)
def test_broker_backoff_doubles_between_5_and_60_seconds(previous, expected):
    last_attempt = datetime.now(timezone.utc)
    job = _job(
        last_attempt_at=last_attempt if previous is not None else None,
        next_attempt_at=last_attempt + previous if previous is not None else datetime.now(timezone.utc),
    )
    assert OutboxProcessor._broker_delay(job) == expected


@pytest.mark.asyncio
async def test_broker_down_stops_the_batch_and_commits_the_reschedule(deps):
    outbox_repo, *_ = deps
    uow = AsyncMock()
    outbox_repo.listar_pendientes.side_effect = [[_job()], [_job()], []]

    processor = _processor(deps, FakeOutboxPublisher(available=False), uow=uow)
    assert await processor.process_pending_batch(limit=10) == 0

    assert outbox_repo.reprogramar.await_count == 1
    assert outbox_repo.listar_pendientes.await_count == 1
    uow.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_batch_resumes_once_the_broker_is_back(deps):
    outbox_repo, *_ = deps
    publisher = FakeOutboxPublisher(available=False)
    first = _job()
    outbox_repo.listar_pendientes.side_effect = [[first], [first], []]
    processor = _processor(deps, publisher, uow=AsyncMock())

    assert await processor.process_pending_batch() == 0
    publisher.available = True
    assert await processor.process_pending_batch() == 1
    assert [p.message.id for p in publisher.published] == [first.id]


@pytest.mark.asyncio
async def test_email_grace_still_applies_in_broker_mode(deps):
    outbox_repo, *_ = deps
    outbox_repo.listar_pendientes.return_value = []
    grace = timedelta(seconds=30)

    await _processor(deps, FakeOutboxPublisher(), uow=AsyncMock(), email_grace=grace).process_pending_batch()

    outbox_repo.listar_pendientes.assert_awaited_once_with(limite=1, email_gracia=grace)


@pytest.mark.asyncio
async def test_unroutable_type_is_marked_failed_instead_of_published(deps):
    outbox_repo, *_ = deps
    outbox_repo.session = AsyncMock()
    publisher = FakeOutboxPublisher()
    job = _job("UNKNOWN_TYPE")

    assert await _processor(deps, publisher).process_one(job) is False

    assert job.status == "FAILED"
    assert publisher.published == []
    outbox_repo.reprogramar.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_mode_is_unchanged_and_never_touches_a_publisher(deps):
    outbox_repo, order_ref_repo, sales_client = deps
    sales_client.notify_payment.return_value = {"nuevoEstado": "PAGADO"}
    job = _job(payload={"order_id": "PED-D", "payment_data": {"transaccionId": "T-1"}})

    assert await OutboxProcessor(outbox_repo, order_ref_repo, sales_client).process_one(job) is True

    sales_client.notify_payment.assert_awaited_once_with(
        "PED-D", {"transaccionId": "T-1"}, correlation_id=None
    )
    order_ref_repo.actualizar_estado.assert_awaited_once_with("PED-D", "PAID_NOTIFIED")
    outbox_repo.marcar_procesado.assert_awaited_once_with(job.id)
    outbox_repo.reprogramar.assert_not_awaited()


# -- adapter: reprogramar ------------------------------------------------------------


@pytest.mark.asyncio
async def test_reprogramar_reschedules_without_incrementing_attempts():
    adapter = OutboxPostgresAdapter(MagicMock(flush=AsyncMock()))
    row = _job(attempts=3)
    adapter.obtener_por_id = AsyncMock(return_value=row)
    when = datetime.now(timezone.utc) + timedelta(seconds=10)

    assert await adapter.reprogramar(row.id, when, last_error="broker down") is row

    assert row.attempts == 3
    assert row.status == "PENDING"
    assert row.next_attempt_at == when
    assert row.last_attempt_at is not None
    assert row.last_error == "broker down"
    adapter.session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_reprogramar_of_a_missing_row_returns_none():
    adapter = OutboxPostgresAdapter(MagicMock(flush=AsyncMock()))
    adapter.obtener_por_id = AsyncMock(return_value=None)
    assert await adapter.reprogramar(uuid.uuid4(), datetime.now(timezone.utc)) is None
