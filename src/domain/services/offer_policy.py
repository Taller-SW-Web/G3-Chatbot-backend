"""Validated commercial projections. Provider data never supplies UI extras."""
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def instant(value):
    if not isinstance(value, str):
        raise ValueError("expected an ISO datetime")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("datetime needs timezone")
    return parsed


def amount(value):
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed < 0:
            raise ValueError("invalid price")
        return parsed.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("invalid price") from exc


def price_projection(row, now=None):
    now = now or datetime.now(timezone.utc)
    if row.get("currency") not in (None, "PEN") or (
        row.get("currency") is None and any(row.get(key) is not None for key in ("precio_regular", "precio_oferta"))
    ):
        raise ValueError("unsupported currency")
    if row.get("channel_id") not in (None, "CHATBOT"):
        raise ValueError("incompatible price channel")
    regular = amount(row.get("precio_regular"))
    offer = amount(row.get("precio_oferta"))
    start = instant(row["valid_from"]) if row.get("valid_from") is not None else None
    end = instant(row["valid_until"]) if row.get("valid_until") is not None else None
    if start and end and end <= start:
        raise ValueError("invalid price validity")
    if start and start > now:
        regular = offer = None
    # Legacy rows have no temporal metadata: only a resolved regular price is safe.
    valid_offer = (regular is not None and offer is not None and 0 < offer < regular
                   and start is not None and start <= now and (end is None or now < end))
    return {
        "price": offer if valid_offer else regular,
        "regular_price": f"{regular:.2f}" if regular is not None else None,
        "offer_price": f"{offer:.2f}" if valid_offer else None,
        "savings_pct": int(((regular - offer) / regular * 100).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP)) if valid_offer else None,
        "offer_valid_until": end.isoformat() if valid_offer and end else None,
    }


def promotion_summaries(rows, now=None):
    now = now or datetime.now(timezone.utc)
    summaries = []
    for row in rows:
        start = instant(row.get("valid_from"))
        end = instant(row["valid_until"]) if row.get("valid_until") is not None else None
        if end and end <= start:
            raise ValueError("invalid promotion validity")
        channels = row.get("channels", [])
        if not isinstance(channels, list):
            raise ValueError("invalid promotion channels")
        if row.get("status") != "ACTIVA" or start > now or (end and end <= now):
            continue
        if channels and "CHATBOT" not in channels:
            continue
        if not row.get("promotion_id") or not isinstance(row.get("name"), str) or not row["name"].strip():
            raise ValueError("invalid promotion summary")
        summaries.append({"promotion_id": str(row["promotion_id"]), "name": row["name"],
                          "valid_from": start.isoformat(),
                          "valid_until": end.isoformat() if end else None})
    return summaries
