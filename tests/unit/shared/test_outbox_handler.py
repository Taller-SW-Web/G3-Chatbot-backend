"""Shared per-type dispatch (ADR-0030): parity with the direct worker and the consumer rules."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.shared.domain.module_errors import ModuleRequestError
from src.shared.infrastructure.worker.outbox_handler import (
    EmailSendRetryError,
    HandlerUnavailableError,
    NonRetryableTaskError,
    OutboxTaskHandler,
)

JOB_ID = uuid.uuid4()
PAYMENT = {"order_id": "PED-1", "payment_data": {"transaccionId": "TX-1"}, "correlation_id": "c-1"}


def _handler(*, notification_service=None, feedback_notifier=None, local_status=None):
    outbox_repo, order_ref_repo, sales_client = AsyncMock(), AsyncMock(), AsyncMock()
    order_ref_repo.obtener_por_order_id.return_value = (
        SimpleNamespace(local_status=local_status) if local_status else None
    )
    handler = OutboxTaskHandler(
        outbox_repo, order_ref_repo, sales_client, notification_service, feedback_notifier
    )
    return handler, outbox_repo, order_ref_repo, sales_client


def _email_service(outcome, error=None):
    service = AsyncMock()
    service.send_confirmation.return_value = SimpleNamespace(
        outcome=SimpleNamespace(value=outcome), error=error
    )
    return service


@pytest.mark.asyncio
async def test_direct_payment_notifies_updates_the_order_and_closes_the_row():
    handler, outbox, order_ref, sales = _handler()
    await handler.dispatch(JOB_ID, "NOTIFY_SALES_PAYMENT", PAYMENT)
    sales.notify_payment.assert_awaited_once_with(
        "PED-1", {"transaccionId": "TX-1"}, correlation_id="c-1"
    )
    order_ref.actualizar_estado.assert_awaited_once_with("PED-1", "PAID_NOTIFIED")
    outbox.marcar_procesado.assert_awaited_once_with(JOB_ID)
    order_ref.obtener_por_order_id.assert_not_awaited()  # no redelivery guard in direct mode


@pytest.mark.asyncio
async def test_direct_cancellation_uses_the_defaults_and_closes_the_row():
    handler, outbox, order_ref, sales = _handler()
    await handler.dispatch(JOB_ID, "REQUEST_CANCELLATION", {"order_id": "PED-2"})
    sales.cancel_order.assert_awaited_once_with(
        "PED-2",
        motivo="PAGO_NO_COMPLETADO",
        comentario="Tiempo límite de espera agotado; cancelado por el canal",
        correlation_id=None,
    )
    order_ref.actualizar_estado.assert_awaited_once_with("PED-2", "CANCELLED")
    outbox.marcar_procesado.assert_awaited_once_with(JOB_ID)


@pytest.mark.asyncio
async def test_direct_email_is_closed_by_the_notification_service_only():
    service = AsyncMock()
    handler, outbox, _, _ = _handler(notification_service=service)
    await handler.dispatch(JOB_ID, "SEND_EMAIL", {"order_id": "PED-1", "resend_number": 0})
    task = service.handle_send_email_task.await_args.args[0]
    assert (task.id, task.payload["order_id"]) == (JOB_ID, "PED-1")
    outbox.marcar_procesado.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_email_without_a_service_marks_the_row_processed():
    handler, outbox, _, _ = _handler()
    await handler.dispatch(JOB_ID, "SEND_EMAIL", {"order_id": "PED-1"})
    outbox.marcar_procesado.assert_awaited_once_with(JOB_ID)


@pytest.mark.asyncio
async def test_direct_feedback_email_is_sent_and_closed():
    notifier = AsyncMock()
    handler, outbox, _, _ = _handler(feedback_notifier=notifier)
    await handler.dispatch(JOB_ID, "SEND_FEEDBACK_EMAIL", {"x": 1})
    notifier.send.assert_awaited_once_with({"x": 1})
    outbox.marcar_procesado.assert_awaited_once_with(JOB_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("relayed", [False, True])
async def test_feedback_without_a_notifier_is_a_retryable_unavailability(relayed):
    handler, outbox, _, _ = _handler()
    with pytest.raises(HandlerUnavailableError):
        await handler.dispatch(JOB_ID, "SEND_FEEDBACK_EMAIL", {}, relayed=relayed)
    outbox.marcar_procesado.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_unknown_type_is_marked_processed():
    handler, outbox, _, _ = _handler()
    await handler.dispatch(JOB_ID, "MYSTERY", {})
    outbox.marcar_procesado.assert_awaited_once_with(JOB_ID)


@pytest.mark.asyncio
async def test_relayed_unknown_type_is_not_retryable_and_never_touches_the_row():
    handler, outbox, _, _ = _handler()
    with pytest.raises(NonRetryableTaskError):
        await handler.dispatch(JOB_ID, "MYSTERY", {}, relayed=True)
    outbox.marcar_procesado.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("job_type", ["NOTIFY_SALES_PAYMENT", "REQUEST_CANCELLATION"])
async def test_relayed_sales_tasks_leave_the_outbox_row_to_the_relay(job_type):
    handler, outbox, _, _ = _handler()
    await handler.dispatch(JOB_ID, job_type, PAYMENT, relayed=True)
    assert outbox.method_calls == []


@pytest.mark.asyncio
async def test_a_redelivered_payment_for_an_already_notified_order_has_no_effects():
    handler, _, order_ref, sales = _handler(local_status="PAID_NOTIFIED")
    await handler.dispatch(JOB_ID, "NOTIFY_SALES_PAYMENT", PAYMENT, relayed=True)
    sales.notify_payment.assert_not_awaited()
    order_ref.actualizar_estado.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_first_delivery_of_a_payment_calls_ventas_once():
    handler, _, order_ref, sales = _handler(local_status="PAYMENT_PENDING_NOTIFICATION")
    await handler.dispatch(JOB_ID, "NOTIFY_SALES_PAYMENT", PAYMENT, relayed=True)
    sales.notify_payment.assert_awaited_once()
    order_ref.actualizar_estado.assert_awaited_once_with("PED-1", "PAID_NOTIFIED")


@pytest.mark.asyncio
async def test_a_redelivered_cancellation_for_a_cancelled_order_has_no_effects():
    handler, _, order_ref, sales = _handler(local_status="CANCELLED")
    await handler.dispatch(JOB_ID, "REQUEST_CANCELLATION", {"order_id": "PED-2"}, relayed=True)
    sales.cancel_order.assert_not_awaited()
    order_ref.actualizar_estado.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_ventas_rejection_propagates_for_the_caller_to_classify():
    handler, _, _, sales = _handler()
    sales.notify_payment.side_effect = ModuleRequestError("ventas", 409, "CONFLICTO")
    with pytest.raises(ModuleRequestError):
        await handler.dispatch(JOB_ID, "NOTIFY_SALES_PAYMENT", PAYMENT, relayed=True)


@pytest.mark.asyncio
async def test_a_payload_without_order_id_is_not_retryable():
    handler, _, _, _ = _handler()
    with pytest.raises(NonRetryableTaskError):
        await handler.dispatch(JOB_ID, "NOTIFY_SALES_PAYMENT", {}, relayed=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["SENT", "ALREADY_SENT"])
async def test_relayed_email_uses_the_single_send_step_and_never_marks_the_row(outcome):
    service = _email_service(outcome)
    handler, outbox, _, _ = _handler(notification_service=service)
    await handler.dispatch(
        JOB_ID, "SEND_EMAIL", {"order_id": "PED-1", "resend_number": 2}, relayed=True
    )
    service.send_confirmation.assert_awaited_once_with("PED-1", 2)
    service.handle_send_email_task.assert_not_awaited()
    assert outbox.method_calls == []


@pytest.mark.asyncio
async def test_relayed_email_retry_outcome_is_retryable():
    handler, _, _, _ = _handler(notification_service=_email_service("RETRY", "smtp down"))
    with pytest.raises(EmailSendRetryError):
        await handler.dispatch(JOB_ID, "SEND_EMAIL", {"order_id": "PED-1"}, relayed=True)


@pytest.mark.asyncio
async def test_relayed_email_failed_outcome_is_not_retryable():
    handler, _, _, _ = _handler(notification_service=_email_service("FAILED", "no snapshot"))
    with pytest.raises(NonRetryableTaskError, match="no snapshot"):
        await handler.dispatch(JOB_ID, "SEND_EMAIL", {"order_id": "PED-1"}, relayed=True)


@pytest.mark.asyncio
async def test_relayed_email_without_a_service_is_unavailable_not_silently_processed():
    handler, _, _, _ = _handler()
    with pytest.raises(HandlerUnavailableError):
        await handler.dispatch(JOB_ID, "SEND_EMAIL", {"order_id": "PED-1"}, relayed=True)
