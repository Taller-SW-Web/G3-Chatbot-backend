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


def _handler(*, notification_service=None, feedback_notifier=None, local_status=None, notify_state="PAGADO"):
    outbox_repo, order_ref_repo, sales_client = AsyncMock(), AsyncMock(), AsyncMock()
    sales_client.notify_payment.return_value = {"pedidoId": "PED-1", "nuevoEstado": notify_state}
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
async def test_a_relayed_payment_reaches_ventas_although_checkout_already_marked_the_order_paid():
    # Checkout marks the order PAID_NOTIFIED when it enqueues the task, so the local
    # status says nothing about Ventas; Ventas deduplicates by `transaccionId` (ADR-0012).
    handler, _, order_ref, sales = _handler(local_status="PAID_NOTIFIED")
    await handler.dispatch(JOB_ID, "NOTIFY_SALES_PAYMENT", PAYMENT, relayed=True)
    sales.notify_payment.assert_awaited_once()
    order_ref.actualizar_estado.assert_awaited_once_with("PED-1", "PAID_NOTIFIED")
    order_ref.obtener_por_order_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_relayed_cancellation_reaches_ventas_although_checkout_already_marked_it_cancelled():
    handler, _, order_ref, sales = _handler(local_status="CANCELLED")
    await handler.dispatch(JOB_ID, "REQUEST_CANCELLATION", {"order_id": "PED-2"}, relayed=True)
    sales.cancel_order.assert_awaited_once()
    order_ref.actualizar_estado.assert_awaited_once_with("PED-2", "CANCELLED")
    order_ref.obtener_por_order_id.assert_not_awaited()


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


# -- payment result from Ventas (decision "Pendiente hasta G5") ---------------------------


def _confirmation_service():
    service = AsyncMock()
    service.enqueue_confirmation.return_value = {"correoEnmascarado": "m***a@ejemplo.com", "mensaje": "ok"}
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("relayed", [False, True])
async def test_pagado_marks_paid_and_enqueues_the_confirmation_in_both_dispatch_modes(relayed):
    service = _confirmation_service()
    handler, _, order_ref, _ = _handler(notification_service=service)
    await handler.dispatch(JOB_ID, "NOTIFY_SALES_PAYMENT", PAYMENT, relayed=relayed)
    order_ref.actualizar_estado.assert_awaited_once_with("PED-1", "PAID_NOTIFIED")
    service.enqueue_confirmation.assert_awaited_once_with("PED-1")


@pytest.mark.asyncio
async def test_a_redelivered_payment_task_does_not_send_a_second_confirmation():
    # `enqueue_confirmation` registers `(order_id, 0)` once; the second call reports the
    # existing notification without a new e-mail task. Here the real service semantics
    # are modelled with a registry: only the first call creates the outbox task.
    created = []

    async def enqueue(order_id):
        if order_id not in created:
            created.append(order_id)
        return {"correoEnmascarado": "m***a@ejemplo.com", "mensaje": "ok"}

    service = AsyncMock()
    service.enqueue_confirmation.side_effect = enqueue
    handler, _, order_ref, sales = _handler(notification_service=service)

    await handler.dispatch(JOB_ID, "NOTIFY_SALES_PAYMENT", PAYMENT, relayed=True)
    await handler.dispatch(JOB_ID, "NOTIFY_SALES_PAYMENT", PAYMENT, relayed=True)

    assert sales.notify_payment.await_count == 2  # Ventas deduplicates by transaccionId
    assert created == ["PED-1"]  # one confirmation for the order


@pytest.mark.asyncio
@pytest.mark.parametrize("relayed", [False, True])
async def test_a_late_payment_on_a_cancelled_order_cancels_locally_without_a_confirmation(relayed, caplog):
    service = _confirmation_service()
    handler, outbox, order_ref, _ = _handler(notification_service=service, notify_state="ANULADO")
    with caplog.at_level("INFO"):
        await handler.dispatch(JOB_ID, "NOTIFY_SALES_PAYMENT", PAYMENT, relayed=relayed)
    order_ref.actualizar_estado.assert_awaited_once_with("PED-1", "CANCELLED")
    service.enqueue_confirmation.assert_not_awaited()
    assert "order_id=PED-1" in caplog.text and "correlation_id=c-1" in caplog.text
    assert outbox.marcar_procesado.await_count == (0 if relayed else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{}, {"nuevoEstado": "MARCIANO"}, None])
async def test_a_payment_answer_without_a_known_state_keeps_the_old_paid_behaviour_with_a_warning(result, caplog):
    service = _confirmation_service()
    handler, _, order_ref, sales = _handler(notification_service=service)
    sales.notify_payment.return_value = result
    with caplog.at_level("WARNING"):
        await handler.dispatch(JOB_ID, "NOTIFY_SALES_PAYMENT", PAYMENT, relayed=True)
    order_ref.actualizar_estado.assert_awaited_once_with("PED-1", "PAID_NOTIFIED")
    service.enqueue_confirmation.assert_awaited_once_with("PED-1")
    assert "without a PAGADO/ANULADO state" in caplog.text


@pytest.mark.asyncio
async def test_a_failing_confirmation_enqueue_propagates_so_the_task_is_retried():
    service = _confirmation_service()
    service.enqueue_confirmation.side_effect = RuntimeError("db down")
    handler, _, _, _ = _handler(notification_service=service)
    with pytest.raises(RuntimeError):
        await handler.dispatch(JOB_ID, "NOTIFY_SALES_PAYMENT", PAYMENT, relayed=True)


# -- 409 PEDIDO_NO_ANULABLE ----------------------------------------------------------------


def _not_cancellable(code="PEDIDO_NO_ANULABLE"):
    return ModuleRequestError("sales", 409, code)


@pytest.mark.asyncio
@pytest.mark.parametrize("relayed", [False, True])
async def test_a_409_not_cancellable_is_a_success_when_ventas_says_anulado(relayed):
    handler, outbox, order_ref, sales = _handler()
    sales.cancel_order.side_effect = _not_cancellable()
    sales.get_order_as_service.return_value = {"pedidoId": "PED-2", "estado": "ANULADO"}

    await handler.dispatch(JOB_ID, "REQUEST_CANCELLATION", {"order_id": "PED-2", "correlation_id": "c-2"}, relayed=relayed)

    sales.get_order_as_service.assert_awaited_once_with("PED-2", correlation_id="c-2")
    order_ref.actualizar_estado.assert_awaited_once_with("PED-2", "CANCELLED")
    assert outbox.marcar_procesado.await_count == (0 if relayed else 1)


@pytest.mark.asyncio
async def test_a_409_not_cancellable_stays_a_failure_when_the_order_is_not_cancelled():
    handler, outbox, order_ref, sales = _handler()
    error = _not_cancellable()
    sales.cancel_order.side_effect = error
    sales.get_order_as_service.return_value = {"pedidoId": "PED-2", "estado": "DESPACHADO"}

    with pytest.raises(ModuleRequestError) as exc:
        await handler.dispatch(JOB_ID, "REQUEST_CANCELLATION", {"order_id": "PED-2"})

    assert exc.value is error
    order_ref.actualizar_estado.assert_not_awaited()
    outbox.marcar_procesado.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_409_whose_confirmation_read_is_refused_keeps_the_original_409():
    handler, _, order_ref, sales = _handler()
    error = _not_cancellable()
    sales.cancel_order.side_effect = error
    sales.get_order_as_service.side_effect = ModuleRequestError("sales", 403, "ACCESO_DENEGADO")

    with pytest.raises(ModuleRequestError) as exc:
        await handler.dispatch(JOB_ID, "REQUEST_CANCELLATION", {"order_id": "PED-2"}, relayed=True)

    assert exc.value is error
    order_ref.actualizar_estado.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_duplicated_cancellation_request_is_not_reconciled():
    handler, _, _, sales = _handler()
    sales.cancel_order.side_effect = _not_cancellable("SOLICITUD_DUPLICADA")

    with pytest.raises(ModuleRequestError):
        await handler.dispatch(JOB_ID, "REQUEST_CANCELLATION", {"order_id": "PED-2"}, relayed=True)

    sales.get_order_as_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_cancellation_errors_are_not_reconciled():
    handler, _, _, sales = _handler()
    sales.cancel_order.side_effect = ModuleRequestError("sales", 404, "PEDIDO_NO_ENCONTRADO")

    with pytest.raises(ModuleRequestError):
        await handler.dispatch(JOB_ID, "REQUEST_CANCELLATION", {"order_id": "PED-2"}, relayed=True)

    sales.get_order_as_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stock_cancelled_order_in_the_fake_ventas_is_cancelled_locally_on_late_payment():
    from src.shared.infrastructure.fakes.fake_ventas import FakeSalesPort

    sales = FakeSalesPort()
    sales.orders["PED-9"] = {
        "pedidoId": "PED-9", "canal": "CHATBOT", "estado": "CREADO", "total": 10, "moneda": "PEN",
        "fechaCreacion": "2026-11-05T19:00:00Z", "items": [], "envio": {},
        "pago": {"estado": "PENDIENTE"},
        "historialEstados": [{"estado": "CREADO", "fecha": "2026-11-05T19:00:00Z", "actor": "X", "motivo": None}],
    }
    sales.cancel_for_stock("PED-9", paid=False)
    service = _confirmation_service()
    handler, _, order_ref, _ = _handler(notification_service=service)
    handler.sales_client = sales

    await handler.dispatch(JOB_ID, "NOTIFY_SALES_PAYMENT", {**PAYMENT, "order_id": "PED-9"}, relayed=True)

    order_ref.actualizar_estado.assert_awaited_once_with("PED-9", "CANCELLED")
    service.enqueue_confirmation.assert_not_awaited()
