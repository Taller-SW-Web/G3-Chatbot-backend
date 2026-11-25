"""UI block helpers shared by the chat tool handlers (contratos-integracion §2.1)."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

LIMA = timezone(timedelta(hours=-5))


def block(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"id": uuid.uuid4().hex, "tipo": kind, "datos": data}


def actions(*items: dict[str, Any]) -> dict[str, Any]:
    return block("ACCIONES_RAPIDAS", {"acciones": list(items)})


def form(kind: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"id": uuid.uuid4().hex, "tipo": "FORMULARIO", "formulario": kind}
    if data:
        result["datos"] = data
    return result


def format_date(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(LIMA).strftime("%d/%m/%Y %H:%M")


def format_money(total: Any, currency: Any) -> str:
    try:
        amount = Decimal(str(total))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    symbol = "S/" if str(currency or "PEN").upper() == "PEN" else str(currency)
    return f"{symbol} {amount:.2f}"
