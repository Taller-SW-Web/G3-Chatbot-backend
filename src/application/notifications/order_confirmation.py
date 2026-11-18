"""Content of the order confirmation e-mail (SPEC-16 Req. 1).

Builds the template context from the checkout `summary` (the snapshot sent
to Ventas): amounts are shown exactly as stored there, never recomputed,
except the line total (unit price x quantity). The e-mail never carries the
document or the full phone number (SPEC-16 RNF Privacidad). Tolerates both
line price keys in use (`precioUnitario` from the H4-A1 contract, `precio`
from the checkout implementation).
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from src.application.use_cases.manage_session_use_case import mask_email
from src.ports.outbound.notification_repository_port import ConfirmationSource

CURRENCY_PREFIX = "S/"
CARD_BRANDS = {"VISA": "Visa", "MASTERCARD": "Mastercard", "AMEX": "Amex", "DINERS": "Diners"}


def confirmation_subject(order_id: str) -> str:
    return f"Confirmamos tu pedido {order_id}"


def masked_card(brand: str | None, last4: str | None) -> str | None:
    """`Visa •••• 1111`; None when the payment attempt is unknown."""
    if not last4:
        return None
    label = CARD_BRANDS.get((brand or "").upper(), (brand or "Tarjeta").title())
    return f"{label} •••• {last4}"


def chat_notice(email: str) -> str:
    """Chat text after the purchase (SPEC-16 Req. 3): never claims the mail arrived."""
    local = mask_email(email).partition("@")[0]
    return f"Te enviaremos la confirmación a {local}@…"


def _amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{CURRENCY_PREFIX} {value:.2f}"


def _variant(line: dict[str, Any]) -> str | None:
    if line.get("variante"):
        return str(line["variante"])
    parts = [str(line[key]) for key in ("talla", "color") if line.get(key)]
    return " / ".join(parts) or None


def _lines(summary: dict[str, Any]) -> list[dict[str, Any]]:
    lines = []
    for raw in summary.get("lineas") or []:
        if not isinstance(raw, dict):
            continue
        quantity = int(raw.get("cantidad") or 1)
        unit = _amount(raw.get("precioUnitario", raw.get("precio")))
        lines.append({
            "nombre": raw.get("nombre") or raw.get("descripcion") or raw.get("sku") or "Producto",
            "variante": _variant(raw),
            "cantidad": quantity,
            "precio_unitario": _money(unit),
            "importe": _money(None if unit is None else unit * quantity),
        })
    return lines


def _mapping(value: Any) -> dict[str, Any]:
    """El valor si es un diccionario; vacío si no lo es (evita `None.get`)."""
    return value if isinstance(value, dict) else {}


def _coupon(summary: dict[str, Any]) -> str | None:
    coupon = summary.get("cupon")
    if isinstance(coupon, dict):
        coupon = coupon.get("codigo") or coupon.get("code")
    return str(coupon) if coupon else None


def _address(summary: dict[str, Any]) -> dict[str, Any] | None:
    direccion = summary.get("direccion") or {}
    if not isinstance(direccion, dict) or not direccion:
        return None
    return {
        "destinatario": direccion.get("destinatario") or direccion.get("nombreCompleto"),
        "linea": direccion.get("direccionExacta") or direccion.get("direccion"),
        "distrito": direccion.get("distrito"),
        "referencia": direccion.get("referencia"),
    }


def _delivery_days(summary: dict[str, Any]) -> int | None:
    envio = _mapping(summary.get("envio"))
    for value in (summary.get("plazoEstimadoDias"), envio.get("plazoEstimadoDias"),
                  envio.get("plazoEstimadoDiasHabiles")):
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def recipient_of(source: ConfirmationSource) -> str | None:
    contacto = source.summary.get("contacto") or {}
    email = contacto.get("email") if isinstance(contacto, dict) else None
    return email.strip() if isinstance(email, str) and "@" in email else None


def build_confirmation_context(
    source: ConfirmationSource, *, order_link: str, customer_name: str | None = None
) -> dict[str, Any]:
    summary = source.summary
    discount = _amount(summary.get("descuento"))
    created: datetime = source.created_at
    contacto = _mapping(summary.get("contacto"))
    return {
        "asunto": confirmation_subject(source.order_id),
        "pedido_id": source.order_id,
        "fecha": created.strftime("%d/%m/%Y"),
        "cliente": customer_name or contacto.get("nombreCompleto"),
        "lineas": _lines(summary),
        "subtotal": _money(_amount(summary.get("subtotal"))),
        "descuento": _money(discount) if discount else None,
        "cupon": _coupon(summary),
        "costo_envio": _money(_amount(summary.get("costoEnvio"))),
        "total": _money(_amount(summary.get("total"))),
        "tarjeta": masked_card(source.card_brand, source.card_last4),
        "direccion": _address(summary),
        "plazo_dias": _delivery_days(summary),
        "enlace_pedido": order_link,
    }
