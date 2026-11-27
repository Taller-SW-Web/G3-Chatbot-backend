"""Cancellation reason and refund message for `ANULADO` orders (backend#182).

G5 only publishes the reason in `historialEstados[estado=ANULADO].motivo` and has
no refund object for cancellations, so the channel derives both values.
"""
from typing import Any

STOCK_NO_DISPONIBLE = "STOCK_NO_DISPONIBLE"
REFUND_MESSAGE = "Se procesará el reembolso al medio de pago original."
_PAYMENT_CONFIRMED = "CONFIRMADO"


def cancellation_reason(history: list[Any]) -> str | None:
    """`motivo` of the latest `ANULADO` history entry, or None when absent."""
    for entry in reversed(history):
        if isinstance(entry, dict) and str(entry.get("estado") or "").upper() == "ANULADO":
            reason = str(entry.get("motivo") or "").strip()
            return reason or None
    return None


def refund_message(
    estado: str, reason: str | None, payment: dict[str, Any], history: list[Any]
) -> str | None:
    """Refund text for a cancelled order, None otherwise.

    Rule: the order is `ANULADO` and money was taken, i.e. the reason is
    `STOCK_NO_DISPONIBLE` (automatic refund), the payment is `CONFIRMADO`, or the
    history shows the order was `PAGADO` before. An order cancelled while unpaid
    (for example `PAGO_NO_COMPLETADO`) has nothing to refund.
    """
    if estado.upper() != "ANULADO":
        return None
    was_paid = (
        reason == STOCK_NO_DISPONIBLE
        or str(payment.get("estado") or "").upper() == _PAYMENT_CONFIRMED
        or any(
            isinstance(entry, dict) and str(entry.get("estado") or "").upper() == "PAGADO"
            for entry in history
        )
    )
    return REFUND_MESSAGE if was_paid else None


def cancellation_message(
    reason: str | None, refund: str | None, pedido_id: str | None = None
) -> str:
    """Customer-facing explanation for tracking and chat (the id is optional)."""
    subject = f"Tu pedido {pedido_id}" if pedido_id else "Tu pedido"
    text = (
        f"{subject} fue anulado por falta de stock."
        if reason == STOCK_NO_DISPONIBLE
        else f"{subject} fue anulado."
    )
    return f"{text} {refund}" if refund else text
