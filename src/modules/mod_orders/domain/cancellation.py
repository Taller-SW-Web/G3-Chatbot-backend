"""Cancellation reason and refund message for `ANULADO` orders (backend#182).

G5 only publishes the reason in `historialEstados[estado=ANULADO].motivo` and has
no refund object for cancellations, so the channel derives both values.
"""
from typing import Any

STOCK_NO_DISPONIBLE = "STOCK_NO_DISPONIBLE"
# Published reason codes only (never free text): the stock cancellation plus the G5 contract enum
# `MotivoAnulacion` (ventas.yaml). Anything else is hidden from the customer.
KNOWN_CANCELLATION_REASONS = frozenset({
    STOCK_NO_DISPONIBLE,
    "PAGO_NO_COMPLETADO",
    "ERROR_SELECCION_PRODUCTO",
    "ARREPENTIMIENTO_COMPRA",
    "SOLICITUD_CLIENTE",
    "ERROR_DATOS_ENVIO",
    "DUPLICADO",
})
REFUND_MESSAGE = "Se procesará el reembolso al medio de pago original."
_PAYMENT_CONFIRMED = "CONFIRMADO"


def safe_reason(value: Any) -> str | None:
    """The reason code when it is a known published code, None for anything else."""
    reason = str(value or "").strip()
    return reason if reason in KNOWN_CANCELLATION_REASONS else None


def cancellation_reason(history: list[Any]) -> str | None:
    """Whitelisted `motivo` of the latest `ANULADO` history entry, None when absent or unknown."""
    for entry in reversed(history):
        if isinstance(entry, dict) and str(entry.get("estado") or "").upper() == "ANULADO":
            return safe_reason(entry.get("motivo"))
    return None


def refund_message(
    estado: str, reason: str | None, payment: dict[str, Any], history: list[Any]
) -> str | None:
    """Refund text for a cancelled order, None otherwise.

    Rule: the order is `ANULADO` and there is payment evidence: `pago.estado` is
    `CONFIRMADO` or the history shows a `PAGADO` entry. The reason never implies a
    refund by itself (a stock cancellation of an unpaid order has nothing to refund).
    """
    if estado.upper() != "ANULADO":
        return None
    was_paid = str(payment.get("estado") or "").upper() == _PAYMENT_CONFIRMED or any(
        isinstance(entry, dict) and str(entry.get("estado") or "").upper() == "PAGADO"
        for entry in history
    )
    return REFUND_MESSAGE if was_paid else None


def cancellation_message(
    reason: str | None, refund: str | None, pedido_id: str | None = None
) -> str:
    """Customer-facing explanation for tracking and chat (the id is optional).

    Only `STOCK_NO_DISPONIBLE` gets a specific text; every other reason is generic.
    """
    subject = f"Tu pedido {pedido_id}" if pedido_id else "Tu pedido"
    text = (
        f"{subject} fue anulado por falta de stock."
        if reason == STOCK_NO_DISPONIBLE
        else f"{subject} fue anulado."
    )
    return f"{text} {refund}" if refund else text
