"""Shared Despacho quote mapping used by the shipping router and the checkout.

Both flows quote through the same `DispatchPort`, so the destination shape, the
cart-line shape and the interpretation of Despacho's answer live here once. The
checkout re-quotes server-side with these helpers; it never trusts a client cost.
"""
from typing import Any, Iterable

from pydantic import ValidationError

from src.application.dto.commerce_contracts_dto import (
    ShippingQuoteRequest,
    ShippingQuoteResponse,
)
from src.ports.outbound.module_errors import ModuleUnavailableError


def build_destination(payload: ShippingQuoteRequest) -> dict[str, Any]:
    """Despacho `destino` from a validated quote request."""
    return {
        "destinatario": payload.destinatario,
        "direccion": payload.direccion,
        "distrito": payload.distrito,
        "departamento": payload.departamento,
        "provincia": payload.provincia,
        "referencia": payload.referencia,
    }


def build_quote_lines(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Despacho `lineas` (sku + cantidad) from cart item rows; empty lines are dropped."""
    return [
        {"sku": row.sku, "cantidad": row.quantity}
        for row in rows
        if row.quantity > 0
    ]


def parse_quote(raw: dict[str, Any], distrito: str) -> ShippingQuoteResponse | None:
    """Interpret Despacho's quote body.

    Returns `None` when the destination has no coverage. Raises
    `ModuleUnavailableError("dispatch")` when the body lacks a usable cost or
    lead time, because the channel cannot invent either.
    """
    if raw.get("coberturaDisponible") is False:
        return None
    try:
        plazo = raw.get("plazoEstimadoDias", raw.get("plazoEstimadoDiasHabiles"))
        return ShippingQuoteResponse(
            coberturaDisponible=True,
            costoEnvio=raw["costoEnvio"],
            moneda=raw.get("moneda", "PEN"),
            plazoEstimadoDias=plazo,
            fechaEstimadaEntrega=raw.get("fechaEstimadaEntrega"),
            nombreZona=raw.get("nombreZona"),
            modalidad=raw.get("modalidad", "DELIVERY"),
            distrito=distrito,
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ModuleUnavailableError("dispatch", "Invalid quote response") from exc
