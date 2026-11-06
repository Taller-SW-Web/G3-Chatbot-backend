"""Tracking payload sanitizer (`SPEC-18` Req. 3 & RNF Privacy).

Enforces the strict whitelist on Despacho tracking responses so GPS coordinates,
driver details, full street addresses and internal failure reasons never reach
either the frontend UI or the LLM context. Furthermore, `recibidoPor` (third-party
recipient name) is allowed only for the frontend UI block and is stripped before
sending data to the LLM.
"""
from typing import Any

from src.domain.services.status_mapper import DISPATCH_STATUS_LABELS


def sanitize_tracking_payload(
    raw: dict[str, Any],
    *,
    pedido_id: str | None = None,
    for_llm: bool = False,
) -> dict[str, Any]:
    """Filters a raw Despacho tracking dictionary down to the `SPEC-18` whitelist."""
    estado = str(raw.get("estado") or "").strip().upper()
    rescheduled_date = raw.get("fechaReprogramada")
    if estado == "PENDIENTE_ASIGNACION" and rescheduled_date:
        default_label = f"Entrega reprogramada para {rescheduled_date}"
    else:
        default_label = DISPATCH_STATUS_LABELS.get(estado, estado or "En preparación")

    etiqueta = str(raw.get("estadoEtiqueta") or default_label)

    raw_milestones = raw.get("hitos")
    hitos: list[dict[str, Any]] = []
    if isinstance(raw_milestones, list):
        for item in raw_milestones:
            if not isinstance(item, dict):
                continue
            titulo = str(item.get("titulo") or item.get("etapa") or "").strip()
            fecha = str(item.get("fecha") or item.get("timestamp") or "").strip()
            if not titulo:
                continue
            hitos.append(
                {
                    "titulo": titulo,
                    "fecha": fecha,
                    "completado": bool(item.get("completado", True)),
                }
            )

    clean: dict[str, Any] = {
        "pedidoId": str(pedido_id or raw.get("pedidoId") or raw.get("idPedido") or ""),
        "estado": estado,
        "estadoEtiqueta": etiqueta,
        "fechaProgramada": raw.get("fechaProgramada") or raw.get("fechaEstimadaEntrega"),
        "distrito": raw.get("distrito"),
        "hitos": hitos,
        "detalleDisponible": bool(raw.get("detalleDisponible", True)),
        "mensaje": raw.get("mensaje"),
    }

    if not for_llm:
        clean["recibidoPor"] = raw.get("recibidoPor")

    return clean
