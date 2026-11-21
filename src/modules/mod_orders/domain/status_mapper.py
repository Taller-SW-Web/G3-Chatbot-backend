"""Order and dispatch status mapping for the customer (`contratos-integracion.md` §4).

Ventas owns the authoritative order status (`CREADO`, `PAGADO`, `EN_PREPARACION`,
`DESPACHADO`, `ENTREGADO`, `ANULADO`). When the order is `DESPACHADO`, Despacho
refines the transit label (`PENDIENTE_ASIGNACION`, `ASIGNADO`, `EN_CAMINO`,
`FALLIDO`, `DEVUELTO_A_ORIGEN`, `ENTREGADO`, `CANCELADO`).
"""

SALES_STATUS_LABELS: dict[str, str] = {
    "CREADO": "Pendiente de pago",
    "PAGADO": "Pago confirmado",
    "EN_PREPARACION": "En preparación",
    "DESPACHADO": "Despachado",
    "ENTREGADO": "Entregado",
    "ANULADO": "Anulado",
}

DISPATCH_STATUS_LABELS: dict[str, str] = {
    "PENDIENTE_ASIGNACION": "En centro de despacho",
    "ASIGNADO": "Asignado a repartidor",
    "EN_CAMINO": "En camino",
    "FALLIDO": "No entregado, regresando al centro",
    "DEVUELTO_A_ORIGEN": "Devuelto al centro de despacho (te contactaremos)",
    "ENTREGADO": "Entregado",
    "CANCELADO": "Anulado",
}


def map_order_status_label(
    sales_status: str,
    dispatch_status: str | None = None,
    *,
    rescheduled_date: str | None = None,
    delivered_date: str | None = None,
) -> str:
    """Maps the Ventas status (and optional Despacho status) to the customer-facing Spanish label.

    Coherence rule (`SPEC-18` RNF): Ventas owns the order status; if Ventas says
    `ENTREGADO` or `ANULADO`, that wins even if Despacho has not caught up.
    """
    normalized_sales = (sales_status or "").strip().upper()
    normalized_dispatch = (dispatch_status or "").strip().upper() if dispatch_status else None

    if normalized_sales == "ENTREGADO":
        return f"Entregado el {delivered_date}" if delivered_date else "Entregado"
    if normalized_sales == "ANULADO":
        return "Anulado"

    if normalized_sales == "DESPACHADO" and normalized_dispatch:
        if normalized_dispatch == "PENDIENTE_ASIGNACION" and rescheduled_date:
            return f"Entrega reprogramada para {rescheduled_date}"
        if normalized_dispatch == "ENTREGADO":
            return f"Entregado el {delivered_date}" if delivered_date else "Entregado"
        if normalized_dispatch in DISPATCH_STATUS_LABELS:
            return DISPATCH_STATUS_LABELS[normalized_dispatch]

    return SALES_STATUS_LABELS.get(normalized_sales, normalized_sales or "Desconocido")
