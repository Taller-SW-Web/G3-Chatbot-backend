"""Server-side ordering of catalog summaries (SPEC-06).

Productos only sorts by name and ignores price; the channel applies `orden`
over the returned page so the parameter has a real effect. The order is
page-scoped until Productos publishes price ordering (A16).
"""
from __future__ import annotations

from typing import Any

RELEVANCE = "RELEVANCIA"
PRICE_ASC = "PRECIO_ASC"
PRICE_DESC = "PRECIO_DESC"
NAME_ASC = "NOMBRE_ASC"
NAME_DESC = "NOMBRE_DESC"
NOVELTY = "NOVEDAD"

ALIASES = {
    "relevancia": RELEVANCE,
    "precio_asc": PRICE_ASC,
    "precio_desc": PRICE_DESC,
    "nombre_asc": NAME_ASC,
    "nombre_desc": NAME_DESC,
    "RELEVANCIA": RELEVANCE,
    "PRECIO_ASC": PRICE_ASC,
    "PRECIO_DESC": PRICE_DESC,
    "NOMBRE_ASC": NAME_ASC,
    "NOMBRE_DESC": NAME_DESC,
    "NOVEDAD": NOVELTY,
}
ALLOWED_ORDER = ("relevancia", "precio_asc", "precio_desc", "nombre_asc", "nombre_desc")


def normalize_order(value: str | None) -> str:
    if not value:
        return RELEVANCE
    normalized = ALIASES.get(value.strip(), ALIASES.get(value.strip().lower(), RELEVANCE))
    return normalized


def sort_summaries(rows: list[dict[str, Any]], order: str | None) -> list[dict[str, Any]]:
    """Stable order over one page; unknown/relevance keeps the upstream order."""
    normalized = normalize_order(order)
    if normalized in (RELEVANCE, NOVELTY):
        return list(rows)
    if normalized in (PRICE_ASC, PRICE_DESC):
        priced = [row for row in rows if row.get("price") is not None]
        unpriced = [row for row in rows if row.get("price") is None]
        priced.sort(key=lambda row: row["price"], reverse=normalized == PRICE_DESC)
        return [*priced, *unpriced]
    reverse = normalized == NAME_DESC
    return sorted(rows, key=lambda row: str(row.get("name") or "").casefold(), reverse=reverse)
