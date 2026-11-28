"""DTOs and mappings for G5 Postventa (F3, F5, F6).

The chatbot exposes its own `camelCase` API and translates to the G5 enums
documented in `ventas.yaml` 1.10.0 / `postventa.yaml` 1.6.0.
"""
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PostventaError(Exception):
    """A controlled postventa failure mapped to `application/problem+json`."""

    def __init__(self, code: str, detail: str | None = None, *, extra: dict[str, Any] | None = None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.extra = extra or {}


POSTVENTA_ERROR_STATUS: dict[str, int] = {
    "VALIDACION": 400,
    "DATOS_INVALIDOS": 400,
    "FORMATO_INVALIDO": 400,
    "DOCUMENTO_INVALIDO": 400,
    "EVIDENCIA_REQUERIDA": 400,
    "PLAZO_VENCIDO": 400,
    "EVIDENCIA_INVALIDA": 400,
    "ITEM_INVALIDO": 400,
    "PAGINACION_INVALIDA": 400,
    "CODIGO_INVALIDO": 400,
    "CANTIDAD_INVALIDA": 400,
    "REQUIERE_SESION": 401,
    "TOKEN_INVALIDO": 401,
    "ACCESO_DENEGADO": 403,
    "RECURSO_NO_ENCONTRADO": 404,
    "PEDIDO_NO_ENCONTRADO": 404,
    "DEVOLUCION_NO_ENCONTRADA": 404,
    "RECLAMO_NO_ENCONTRADO": 404,
    "PEDIDO_NO_ENTREGADO": 409,
    "DEVOLUCION_EN_CURSO": 409,
    "CSAT_ALREADY_SUBMITTED": 409,
    "RECLAMO_YA_ATENDIDO": 409,
    "RECLAMO_DUPLICADO": 409,
    "DEVOLUCION_DUPLICADA": 409,
    "ORDER_NOT_DELIVERED": 422,
    "DEMASIADAS_SOLICITUDES": 429,
    "MODULO_NO_DISPONIBLE": 503,
    "SERVICIO_NO_DISPONIBLE": 503,
}

# G3 UI codes -> G5 enums (agreed 2026-11-17).
RETURN_REASON_TO_G5: dict[str, str] = {
    "TALLA_INCORRECTA": "OTRO",
    "NO_ERA_LO_QUE_ESPERABA": "OTRO",
    "PRODUCTO_DEFECTUOSO": "PRODUCTO_DEFECTUOSO",
    "PRODUCTO_EQUIVOCADO_ENVIADO": "PRODUCTO_INCORRECTO",
    "YA_NO_LO_QUIERO": "OTRO",
    "OTRO": "OTRO",
}

CLAIM_REASONS: frozenset[str] = frozenset(
    {
        "INCUMPLIMIENTO_PLAZO_ENTREGA",
        "PRODUCTO_DEFECTUOSO_O_INCORRECTO",
        "COBRO_INDEBIDO_O_NO_RECONOCIDO",
        "ATENCION_INADECUADA",
        "INCUMPLIMIENTO_GARANTIA",
    }
)
ClaimReason = Literal[
    "INCUMPLIMIENTO_PLAZO_ENTREGA",
    "PRODUCTO_DEFECTUOSO_O_INCORRECTO",
    "COBRO_INDEBIDO_O_NO_RECONOCIDO",
    "ATENCION_INADECUADA",
    "INCUMPLIMIENTO_GARANTIA",
]

RETURN_TYPES = {"CAMBIO", "DEVOLUCION_DINERO"}
CLAIM_TYPES = {"RECLAMO", "QUEJA"}
EVIDENCE_TYPES = {"IMAGEN", "DOCUMENTO"}


def to_g5_return_reason(value: str) -> str:
    normalized = (value or "").strip().upper()
    return RETURN_REASON_TO_G5.get(normalized, normalized)


def to_g5_claim_reason(value: str) -> str:
    """Only the five G5 values are accepted; legacy aliases are rejected (not translated)."""
    normalized = (value or "").strip().upper()
    if normalized not in CLAIM_REASONS:
        raise ValueError("Motivo de reclamo inválido")
    return normalized


def to_g5_return_type(value: str) -> str:
    normalized = (value or "").strip().upper()
    if normalized in {"DEVOLUCION", "DEVOLUCION_DINERO"}:
        return "DEVOLUCION_DINERO"
    return "CAMBIO"


def to_g5_evidence_type(value: str) -> str:
    normalized = (value or "").strip().upper()
    if normalized in {"PDF", "DOCUMENT", "DOCUMENTO"}:
        return "DOCUMENTO"
    return "IMAGEN"


class ReturnItemDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    productoId: str = Field(..., min_length=1)
    cantidad: int = Field(..., ge=1)


class ReturnEvidenceDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tipo: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1, max_length=500)

    @field_validator("tipo")
    @classmethod
    def _normalize_type(cls, value: str) -> str:
        return to_g5_evidence_type(value)


class CreateReturnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pedidoId: str = Field(..., min_length=1)
    tipo: str = Field(..., min_length=1)
    motivo: str = Field(..., min_length=1)
    descripcion: str | None = Field(default=None, max_length=2000)
    items: list[ReturnItemDTO] = Field(..., min_length=1)
    evidencias: list[ReturnEvidenceDTO] = Field(default_factory=list)
    # Optional conversation that owns the 24 h draft; never forwarded to G5.
    conversacionId: UUID | None = None
    # SPEC-21: register anyway after the duplicate warning.
    forzar: bool = False

    @field_validator("tipo")
    @classmethod
    def _normalize_return_type(cls, value: str) -> str:
        normalized = to_g5_return_type(value)
        if normalized not in RETURN_TYPES:
            raise ValueError("Tipo de devolución inválido")
        return normalized

    @field_validator("motivo")
    @classmethod
    def _normalize_reason(cls, value: str) -> str:
        return to_g5_return_reason(value)


class ReturnItemResponseDTO(BaseModel):
    """G5 item echoed back: lenient, so extra G5 fields never turn a created return into a 503."""

    model_config = ConfigDict(extra="ignore")

    productoId: str
    cantidad: int


class ReturnEvidenceResponseDTO(BaseModel):
    """G5 evidence echoed back: no request-side limits (url length) and extra fields ignored."""

    model_config = ConfigDict(extra="ignore")

    tipo: str
    url: str


class ReturnSummaryDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    devolucionId: str
    pedidoId: str
    tipo: str
    estado: str
    motivo: str
    estadoReembolso: str | None = None
    fechaRegistro: str


class ReturnListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ReturnSummaryDTO] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    page: int = Field(default=1, ge=1)
    pageSize: int = Field(default=10, ge=1, le=50)


class ReturnDetailResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    devolucionId: str
    pedidoId: str
    tipo: str
    estado: str
    motivo: str
    descripcion: str | None = None
    items: list[ReturnItemResponseDTO] = Field(default_factory=list)
    resolucion: dict[str, Any] | None = None
    evidencias: list[ReturnEvidenceResponseDTO] = Field(default_factory=list)
    fechaRegistro: str
    historial: list[dict[str, Any]] = Field(default_factory=list)
    recepciones: list[dict[str, Any]] = Field(default_factory=list)


class ClaimConsumerDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nombreCompleto: str = Field(..., min_length=1, max_length=150)
    documento: str = Field(..., min_length=1, max_length=20)
    email: str = Field(..., min_length=3, max_length=150)
    telefono: str = Field(..., min_length=1, max_length=20)


class CreateClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pedidoId: str | None = Field(default=None, min_length=1)
    tipo: str = Field(..., min_length=1)
    motivo: ClaimReason
    detalle: str = Field(..., min_length=20, max_length=1000)
    consumidor: ClaimConsumerDTO
    solucionEsperada: str | None = Field(default=None, max_length=500)
    # Optional conversation that owns the 24 h draft; never forwarded to G5.
    conversacionId: UUID | None = None
    # SPEC-19: register anyway after the duplicate warning.
    forzar: bool = False

    @field_validator("tipo")
    @classmethod
    def _normalize_claim_type(cls, value: str) -> str:
        normalized = (value or "").strip().upper()
        if normalized not in CLAIM_TYPES:
            raise ValueError("Tipo de reclamo inválido")
        return normalized

    @field_validator("motivo", mode="before")
    @classmethod
    def _normalize_reason(cls, value: Any) -> str:
        return to_g5_claim_reason(str(value or ""))


class ClaimSummaryDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reclamoId: str
    codigoSeguimiento: str
    pedidoId: str | None = None
    tipo: str
    motivo: str
    estado: str
    fechaRegistro: str
    fechaLimiteSLA: str | None = None
    estadoSla: str | None = None
    diasHabilesRestantes: int | None = None


class ClaimListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ClaimSummaryDTO] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    page: int = Field(default=1, ge=1)
    pageSize: int = Field(default=10, ge=1, le=50)


class ClaimDetailResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reclamoId: str
    codigoSeguimiento: str
    pedidoId: str | None = None
    tipo: str
    canal: str = "CHATBOT"
    motivo: str
    detalle: str
    estado: str
    plazoDiasHabiles: int = 15
    fechaLimiteSLA: str
    fechaRegistro: str
    fechaAtencion: str | None = None
    respuestaVisibleCliente: str | None = None
    estadoSla: str | None = None
    diasHabilesRestantes: int | None = None
    slaCumplido: bool | None = None


class CreateCsatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    puntuacion: Literal[1, 3, 5]
    comentario: str | None = Field(default=None, max_length=1000)


class EvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tipo: str
    url: str
    nombreArchivoOriginal: str
    tamanioBytes: int = Field(..., ge=1)
    fechaSubida: str
