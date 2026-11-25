"""Customer-facing labels for G5 post-sale states (SPEC-20, SPEC-22)."""

CLAIM_STATUS_LABELS: dict[str, str] = {
    "REGISTRADO": "Recibido",
    "EN_PROCESO": "En revisión",
    "ATENDIDO": "Respondido",
    "DERIVADO": "Derivado a otro equipo",
}

OPEN_CLAIM_STATES = frozenset({"REGISTRADO", "EN_PROCESO"})

RETURN_STATUS_LABELS: dict[str, str] = {
    "SOLICITADA": "Recibida",
    "EN_EVALUACION": "En revisión",
    "APROBADA": "Aprobada",
    "RECHAZADA": "Rechazada",
    "COMPLETADA": "Completada",
}


def map_claim_status_label(status: object) -> str:
    normalized = str(status or "").strip().upper()
    return CLAIM_STATUS_LABELS.get(normalized, normalized or "Desconocido")


def map_return_status_label(status: object) -> str:
    normalized = str(status or "").strip().upper()
    return RETURN_STATUS_LABELS.get(normalized, normalized or "Desconocido")
