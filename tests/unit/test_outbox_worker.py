"""Tests for OutboxProcessor (SPEC-15 Req. 2, 3)."""
from datetime import datetime, timezone
import uuid
import pytest
from unittest.mock import AsyncMock

from src.adapters.outbound.persistence.models.outbox import Outbox
from src.infrastructure.worker.outbox_worker import OutboxProcessor
from src.ports.outbound.module_errors import ModuleRequestError, ModuleUnavailableError


@pytest.fixture
def mock_repos():
    outbox_repo = AsyncMock()
    order_ref_repo = AsyncMock()
    sales_client = AsyncMock()
    return outbox_repo, order_ref_repo, sales_client


@pytest.mark.asyncio
async def test_notify_sales_payment_success(mock_repos):
    outbox_repo, order_ref_repo, sales_client = mock_repos
    processor = OutboxProcessor(outbox_repo, order_ref_repo, sales_client)

    job = Outbox(
        id=uuid.uuid4(),
        type="NOTIFY_SALES_PAYMENT",
        payload={"order_id": "PED-1", "payment_data": {"transaccionId": "SIM-1"}},
        status="PENDING",
        attempts=0,
    )

    success = await processor.process_one(job)
    assert success is True
    sales_client.notify_payment.assert_awaited_once_with(
        "PED-1", {"transaccionId": "SIM-1"}, correlation_id=None
    )
    order_ref_repo.actualizar_estado.assert_awaited_once_with("PED-1", "PAID_NOTIFIED")
    outbox_repo.marcar_procesado.assert_awaited_once_with(job.id)


@pytest.mark.asyncio
async def test_request_cancellation_success(mock_repos):
    outbox_repo, order_ref_repo, sales_client = mock_repos
    processor = OutboxProcessor(outbox_repo, order_ref_repo, sales_client)

    job = Outbox(
        id=uuid.uuid4(),
        type="REQUEST_CANCELLATION",
        payload={"order_id": "PED-2", "motivo": "PAGO_NO_COMPLETADO"},
        status="PENDING",
        attempts=0,
    )

    success = await processor.process_one(job)
    assert success is True
    sales_client.cancel_order.assert_awaited_once_with(
        "PED-2",
        motivo="PAGO_NO_COMPLETADO",
        comentario="Tiempo límite de espera agotado; cancelado por el canal",
        correlation_id=None,
    )
    order_ref_repo.actualizar_estado.assert_awaited_once_with("PED-2", "CANCELLED")
    outbox_repo.marcar_procesado.assert_awaited_once_with(job.id)


@pytest.mark.asyncio
async def test_permanent_error_409_marks_failed_immediately(mock_repos):
    outbox_repo, order_ref_repo, sales_client = mock_repos
    sales_client.notify_payment.side_effect = ModuleRequestError("sales", 409, "ESTADO_INVALIDO")

    processor = OutboxProcessor(outbox_repo, order_ref_repo, sales_client)

    job = Outbox(
        id=uuid.uuid4(),
        type="NOTIFY_SALES_PAYMENT",
        payload={"order_id": "PED-3"},
        status="PENDING",
        attempts=0,
    )

    success = await processor.process_one(job)
    assert success is False
    assert job.status == "FAILED"
    assert job.attempts == 5
    outbox_repo.marcar_reintento.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_error_schedules_retry(mock_repos):
    outbox_repo, order_ref_repo, sales_client = mock_repos
    sales_client.notify_payment.side_effect = ModuleUnavailableError("sales", "Timeout")

    processor = OutboxProcessor(outbox_repo, order_ref_repo, sales_client)

    job = Outbox(
        id=uuid.uuid4(),
        type="NOTIFY_SALES_PAYMENT",
        payload={"order_id": "PED-4"},
        status="PENDING",
        attempts=0,
    )

    success = await processor.process_one(job)
    assert success is False
    outbox_repo.marcar_reintento.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_email_delegates_to_notification_service(mock_repos):
    outbox_repo, order_ref_repo, sales_client = mock_repos
    notif_svc = AsyncMock()
    processor = OutboxProcessor(
        outbox_repo, order_ref_repo, sales_client, notification_service=notif_svc
    )

    job_id = uuid.uuid4()
    job = Outbox(
        id=job_id,
        type="SEND_EMAIL",
        payload={"order_id": "PED-EMAIL-1", "resend_number": 0},
        status="PENDING",
        attempts=0,
    )

    success = await processor.process_one(job)
    assert success is True
    notif_svc.handle_send_email_task.assert_awaited_once()
    task_arg = notif_svc.handle_send_email_task.call_args[0][0]
    assert task_arg.id == job_id
    assert task_arg.payload["order_id"] == "PED-EMAIL-1"


@pytest.mark.asyncio
async def test_process_pending_batch(mock_repos):
    outbox_repo, order_ref_repo, sales_client = mock_repos
    job1 = Outbox(
        id=uuid.uuid4(),
        type="NOTIFY_SALES_PAYMENT",
        payload={"order_id": "PED-BATCH-1"},
        status="PENDING",
        attempts=0,
    )
    job2 = Outbox(
        id=uuid.uuid4(),
        type="REQUEST_CANCELLATION",
        payload={"order_id": "PED-BATCH-2"},
        status="PENDING",
        attempts=0,
    )
    outbox_repo.listar_pendientes.return_value = [job1, job2]

    processor = OutboxProcessor(outbox_repo, order_ref_repo, sales_client)
    count = await processor.process_pending_batch(limit=10)
    assert count == 2
    assert sales_client.notify_payment.await_count == 1
    assert sales_client.cancel_order.await_count == 1

