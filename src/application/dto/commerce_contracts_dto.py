"""HTTP and application contract DTOs for Hito 4 commerce flows.

Covers the four domains required by H4-A1 (`contratos-integracion.md` §2.4, §2.5, §2.8):
1. Coupons (`SPEC-13`): `POST /carrito/cupon`, `DELETE /carrito/cupon`.
2. Shipping & Addresses (`SPEC-12`): `GET /direcciones`, `POST /envio/cotizar`.
3. Checkout & Payment Simulation (`SPEC-14`, `SPEC-15`): `POST /checkout`,
   `GET /checkout/{checkoutId}`, `POST /checkout/{checkoutId}/pago`.
4. Orders & Dispatch Tracking (`SPEC-15..18`): `GET /pedidos`,
   `GET /pedidos/{pedidoId}`, `GET /pedidos/{pedidoId}/seguimiento`,
   `POST /pedidos/{pedidoId}/reenviar-confirmacion`.
"""
import re
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# 1. Buyer identity document rules (SPEC-12 Req. 1, agreement A14 with Ventas)
# ---------------------------------------------------------------------------


class DocumentType(str, Enum):
    DNI = "DNI"
    RUC = "RUC"
    CE = "CE"
    PASAPORTE = "PASAPORTE"


_DOCUMENT_PATTERNS: dict[DocumentType, re.Pattern[str]] = {
    DocumentType.DNI: re.compile(r"^[0-9]{8}$"),
    DocumentType.RUC: re.compile(r"^(10|15|17|20)[0-9]{9}$"),
    DocumentType.CE: re.compile(r"^[a-zA-Z0-9]{8,12}$"),
    DocumentType.PASAPORTE: re.compile(r"^[a-zA-Z0-9]{6,12}$"),
}


def validate_buyer_document(tipo_documento: str, numero_documento: str) -> tuple[str, str]:
    """Validates `tipoDocumento` and `numeroDocumento` against Ventas rules (A14).

    Raises `ValueError("DATO_INVALIDO")` when the type is unknown or the number
    does not match its pattern (including the `10|15|17|20` prefix rule for RUC).
    """
    raw_type = (tipo_documento or "").strip().upper()
    raw_number = (numero_documento or "").strip()
    try:
        doc_type = DocumentType(raw_type)
    except ValueError as exc:
        raise ValueError("DATO_INVALIDO") from exc
    if not raw_number or not _DOCUMENT_PATTERNS[doc_type].match(raw_number):
        raise ValueError("DATO_INVALIDO")
    return doc_type.value, raw_number


class BuyerContactDTO(BaseModel):
    """Buyer contact and identity document snapshot (`SPEC-12` Req. 1, `SPEC-15` Req. 1)."""

    model_config = ConfigDict(extra="ignore")

    nombreCompleto: str = Field(..., min_length=1, max_length=150)
    tipoDocumento: str
    numeroDocumento: str
    telefono: str | None = None
    email: str | None = None

    @field_validator("nombreCompleto")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        clean = v.strip()
        if not clean:
            raise ValueError("El nombre completo es obligatorio.")
        return clean

    @model_validator(mode="after")
    def _check_document(self) -> "BuyerContactDTO":
        doc_type, doc_num = validate_buyer_document(self.tipoDocumento, self.numeroDocumento)
        self.tipoDocumento = doc_type
        self.numeroDocumento = doc_num
        return self


# ---------------------------------------------------------------------------
# 2. Coupons (`SPEC-13`, `contratos-integracion.md` §2.4.1)
# ---------------------------------------------------------------------------


class CouponRejectionReason(str, Enum):
    INEXISTENTE = "INEXISTENTE"
    EXPIRADO = "EXPIRADO"
    NO_VIGENTE = "NO_VIGENTE"
    AGOTADO = "AGOTADO"
    LIMITE_CLIENTE = "LIMITE_CLIENTE"
    MONTO_MINIMO = "MONTO_MINIMO"
    NO_APLICA_PRODUCTOS = "NO_APLICA_PRODUCTOS"
    NO_APLICA_CANAL = "NO_APLICA_CANAL"
    NO_COMBINABLE = "NO_COMBINABLE"


# Translates Productos HTTP 0.5.0 rejection `code` values (`SPEC-13` design.md)
# to the canonical `motivo` exposed by the chatbot contract.
PRODUCTS_TO_COUPON_REASON: dict[str, CouponRejectionReason] = {
    "CUPON_NO_ENCONTRADO": CouponRejectionReason.INEXISTENTE,
    "INEXISTENTE": CouponRejectionReason.INEXISTENTE,
    "CUPON_FUERA_DE_VIGENCIA": CouponRejectionReason.EXPIRADO,
    "EXPIRADO": CouponRejectionReason.EXPIRADO,
    "CUPON_INACTIVO": CouponRejectionReason.NO_VIGENTE,
    "NO_VIGENTE": CouponRejectionReason.NO_VIGENTE,
    "CUPON_AGOTADO": CouponRejectionReason.AGOTADO,
    "AGOTADO": CouponRejectionReason.AGOTADO,
    "LIMITE_CUPON_CLIENTE_ALCANZADO": CouponRejectionReason.LIMITE_CLIENTE,
    "LIMITE_CLIENTE": CouponRejectionReason.LIMITE_CLIENTE,
    "MONTO_MINIMO_NO_ALCANZADO": CouponRejectionReason.MONTO_MINIMO,
    "MONTO_MINIMO": CouponRejectionReason.MONTO_MINIMO,
    "NO_APLICA_PRODUCTOS": CouponRejectionReason.NO_APLICA_PRODUCTOS,
    "NO_APLICA_CANAL": CouponRejectionReason.NO_APLICA_CANAL,
    "CUPON_NO_COMBINABLE": CouponRejectionReason.NO_COMBINABLE,
    "NO_COMBINABLE": CouponRejectionReason.NO_COMBINABLE,
}

COUPON_REJECTION_MESSAGES: dict[CouponRejectionReason, str] = {
    CouponRejectionReason.INEXISTENTE: "Ese cupón no existe. Revisa que esté bien escrito.",
    CouponRejectionReason.EXPIRADO: "Ese cupón ya no está vigente.",
    CouponRejectionReason.NO_VIGENTE: "Ese cupón ya no está vigente.",
    CouponRejectionReason.AGOTADO: "Ese cupón ya alcanzó su límite de usos.",
    CouponRejectionReason.LIMITE_CLIENTE: "Ya usaste este cupón el máximo de veces permitido.",
    CouponRejectionReason.MONTO_MINIMO: "Este cupón no alcanza el monto mínimo requerido.",
    CouponRejectionReason.NO_APLICA_PRODUCTOS: (
        "Este cupón no aplica a los productos de tu carrito."
    ),
    CouponRejectionReason.NO_APLICA_CANAL: "Este cupón no es válido en este canal.",
    CouponRejectionReason.NO_COMBINABLE: (
        "Tu carrito ya tiene un beneficio mejor que no se combina con este cupón."
    ),
}

COUPON_ERROR_STATUS: dict[str, int] = {
    "VALIDACION": 400,
    "REQUIERE_SESION": 401,
    "RECURSO_NO_ENCONTRADO": 404,
    "REQUIERE_CONFIRMACION_REEMPLAZO": 409,
    "CARRITO_DESACTUALIZADO": 409,
    "CUPON_INVALIDO": 422,
    "DEMASIADAS_SOLICITUDES": 429,
    "MODULO_NO_DISPONIBLE": 503,
    "SERVICIO_NO_DISPONIBLE": 503,
}


class ApplyCouponRequest(BaseModel):
    """Input for `POST /api/v1/carrito/cupon` (`SPEC-13` Req. 1)."""

    model_config = ConfigDict(extra="forbid")

    codigo: str = Field(..., min_length=1, max_length=40)
    confirmarReemplazo: bool = False

    @field_validator("codigo", mode="before")
    @classmethod
    def _normalize_code(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("El código del cupón debe ser texto.")
        normalized = v.strip().upper()
        if not normalized:
            raise ValueError("El código del cupón no puede estar vacío.")
        return normalized


class AppliedCouponDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    codigo: str
    descuento: float = Field(..., ge=0)
    moneda: str = "PEN"
    promocionId: str | None = None


class CouponCartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mensaje: str
    cupon: AppliedCouponDTO | None = None
    carrito: dict[str, Any]


# ---------------------------------------------------------------------------
# 3. Shipping & Saved Addresses (`SPEC-12`, `contratos-integracion.md` §2.4.2)
# ---------------------------------------------------------------------------

DISPATCH_TO_CHANNEL_ERROR: dict[str, str] = {
    "DESP_ERROR_DESTINO_REQUERIDO": "VALIDACION",
    "DESP_ERROR_LINEAS_VACIAS": "VALIDACION",
    "DESP_ERROR_CANTIDAD_INVALIDA": "VALIDACION",
    "DESP_ERROR_PRODUCTO_NO_ENCONTRADO": "PRODUCTO_NO_DISPONIBLE",
    "DESP_ERROR_DATOS_FISICOS_INCOMPLETOS": "SERVICIO_NO_DISPONIBLE",
    "DESP_ERROR_PRODUCTOS_NO_DISPONIBLE": "SERVICIO_NO_DISPONIBLE",
    "DESP_ERROR_LIMITE_COTIZACION": "DEMASIADAS_SOLICITUDES",
}

SHIPPING_ERROR_STATUS: dict[str, int] = {
    "VALIDACION": 400,
    "DATO_INVALIDO": 400,
    "REQUIERE_SESION": 401,
    "PRODUCTO_NO_DISPONIBLE": 409,
    "SIN_COBERTURA": 422,
    "DEMASIADAS_SOLICITUDES": 429,
    "MODULO_NO_DISPONIBLE": 503,
    "SERVICIO_NO_DISPONIBLE": 503,
}


class SavedAddressDTO(BaseModel):
    """Saved address returned by `GET /api/v1/direcciones` (Seguridad proxy)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    etiqueta: str | None = None
    destinatario: str | None = None
    departamento: str = "Lima"
    provincia: str = "Lima"
    distrito: str
    direccion: str
    referencia: str | None = None
    predeterminada: bool = False


class SavedAddressesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direcciones: list[SavedAddressDTO] = Field(default_factory=list)


class ShippingQuoteRequest(BaseModel):
    """Input for `POST /api/v1/envio/cotizar` (`SPEC-12` Req. 2 & Req. 4).

    Accepts both the Ventas field names (`destinatario`, `direccion`) and the
    Checkout UI aliases (`nombreCompleto`, `direccionExacta`).
    """

    model_config = ConfigDict(extra="forbid")

    destinatario: str | None = None
    nombreCompleto: str | None = None
    direccion: str | None = None
    direccionExacta: str | None = None
    departamento: str = "Lima"
    provincia: str = "Lima"
    distrito: str = Field(..., min_length=1, max_length=100)
    referencia: str | None = Field(default=None, max_length=250)
    conversacionId: UUID | None = None

    @model_validator(mode="after")
    def _validate_recipient_and_address(self) -> "ShippingQuoteRequest":
        recipient = (self.destinatario or self.nombreCompleto or "").strip()
        street = (self.direccion or self.direccionExacta or "").strip()
        district = (self.distrito or "").strip()
        if not recipient:
            raise ValueError("El destinatario o nombreCompleto es obligatorio.")
        if len(street) < 5:
            raise ValueError("La dirección debe tener al menos 5 caracteres.")
        if not district:
            raise ValueError("El distrito es obligatorio.")
        self.destinatario = recipient
        self.nombreCompleto = recipient
        self.direccion = street
        self.direccionExacta = street
        self.distrito = district
        return self


class ShippingQuoteResponse(BaseModel):
    """Output of `POST /api/v1/envio/cotizar` (`SPEC-12` Req. 4)."""

    model_config = ConfigDict(extra="forbid")

    coberturaDisponible: bool = True
    costoEnvio: float = Field(..., ge=0)
    moneda: str = "PEN"
    plazoEstimadoDias: int = Field(..., ge=0)
    fechaEstimadaEntrega: str | None = None
    nombreZona: str | None = None
    modalidad: str = "DELIVERY"
    distrito: str


# ---------------------------------------------------------------------------
# 4. Checkout & Payment Simulation (`SPEC-14`, `SPEC-15`, §2.5.1)
# ---------------------------------------------------------------------------

CHECKOUT_ERROR_STATUS: dict[str, int] = {
    "VALIDACION": 400,
    "DATO_INVALIDO": 400,
    "REQUIERE_SESION": 401,
    "TOKEN_INVALIDO": 401,
    "PAGO_RECHAZADO": 402,
    "CELULAR_NO_VERIFICADO": 403,
    "CUENTA_NO_PERMITIDA": 403,
    "RECURSO_NO_ENCONTRADO": 404,
    "CARRITO_VACIO": 409,
    "STOCK_INSUFICIENTE": 409,
    "PRODUCTO_NO_DISPONIBLE": 409,
    "CARRITO_DESACTUALIZADO": 409,
    "CHECKOUT_EXPIRADO": 410,
    "SIN_COBERTURA": 422,
    "ERROR_PROCESAMIENTO": 503,
    "MODULO_NO_DISPONIBLE": 503,
    "SERVICIO_NO_DISPONIBLE": 503,
}


class CreateCheckoutRequest(BaseModel):
    """Input for `POST /api/v1/checkout` (`SPEC-14` Req. 2, compatible with PR #59)."""

    model_config = ConfigDict(extra="ignore")

    cartId: UUID | None = None
    conversacionId: UUID | None = None
    guardarDireccion: bool = False
    direccion: dict[str, Any] | None = None
    contacto: dict[str, Any] | None = None
    lineas: list[dict[str, Any]] = Field(default_factory=list)
    subtotal: float = Field(default=0.0, ge=0)
    costoEnvio: float = Field(default=0.0, ge=0)
    descuento: float = Field(default=0.0, ge=0)
    total: float = Field(..., ge=0)


class CheckoutSessionResponse(BaseModel):
    """Output of `POST /api/v1/checkout` and `GET /api/v1/checkout/{checkoutId}`."""

    model_config = ConfigDict(extra="forbid")

    id: str
    cartId: str
    pedidoId: str | None = None
    status: str
    total: float
    moneda: str = "PEN"
    expiresAt: str
    intentosRestantes: int = Field(..., ge=0, le=3)
    summary: dict[str, Any]
    avisoDireccion: str | None = None


class ProcessPaymentRequest(BaseModel):
    """Input for `POST /api/v1/checkout/{checkoutId}/pago` (`SPEC-14` Req. 3).

    Never logged or persisted; only brand and last4 survive the simulation.
    """

    model_config = ConfigDict(extra="forbid")

    numeroTarjeta: str = Field(..., min_length=13, max_length=25)
    titular: str = Field(..., min_length=1, max_length=150)
    vencimiento: str = Field(..., min_length=4, max_length=7)
    cvv: str = Field(..., min_length=3, max_length=4)


class PaymentResultResponse(BaseModel):
    """Output of `POST /api/v1/checkout/{checkoutId}/pago` (`SPEC-14` Req. 5)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    checkoutId: str
    pedidoId: str | None = None
    resultado: str = "APROBADO"
    marca: str
    ultimos4: str = Field(..., min_length=4, max_length=4)
    monto: float
    moneda: str = "PEN"
    idTransaccion: str | None = None


# ---------------------------------------------------------------------------
# 5. Orders & Dispatch Tracking (`SPEC-16`, `SPEC-17`, `SPEC-18`, §2.5.2)
# ---------------------------------------------------------------------------

ORDERS_ERROR_STATUS: dict[str, int] = {
    "VALIDACION": 400,
    "REQUIERE_SESION": 401,
    "TOKEN_INVALIDO": 401,
    "RECURSO_NO_ENCONTRADO": 404,
    "LIMITE_REENVIOS_EXCEDIDO": 429,
    "MODULO_NO_DISPONIBLE": 503,
    "SERVICIO_NO_DISPONIBLE": 503,
}


class OrderSummaryItemDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pedidoId: str
    canal: str = "CHATBOT"
    estado: str
    estadoEtiqueta: str
    total: float
    moneda: str = "PEN"
    fechaCreacion: str


class OrderListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[OrderSummaryItemDTO] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    page: int = Field(default=1, ge=1)
    pageSize: int = Field(default=10, ge=1, le=50)


class OrderLineItemDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    productoId: str
    sku: str
    descripcion: str
    cantidad: int = Field(..., ge=1)
    precioUnitario: float = Field(..., ge=0)


class OrderHistoryMilestoneDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estado: str
    etiqueta: str
    fecha: str
    completado: bool = True


class OrderDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pedidoId: str
    canal: str = "CHATBOT"
    estado: str
    estadoEtiqueta: str
    total: float
    moneda: str = "PEN"
    fechaCreacion: str
    items: list[OrderLineItemDTO] = Field(default_factory=list)
    envio: dict[str, Any] | None = None
    historial: list[OrderHistoryMilestoneDTO] = Field(default_factory=list)
    seguimientoDisponible: bool = False


class TrackingMilestoneDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    titulo: str
    fecha: str
    completado: bool = True


class OrderTrackingResponse(BaseModel):
    """Whitelisted tracking projection (`SPEC-18` Req. 3 & RNF Privacy)."""

    model_config = ConfigDict(extra="forbid")

    pedidoId: str
    estado: str
    estadoEtiqueta: str
    fechaProgramada: str | None = None
    distrito: str | None = None
    hitos: list[TrackingMilestoneDTO] = Field(default_factory=list)
    recibidoPor: str | None = None
    detalleDisponible: bool = True
    mensaje: str | None = None


class ResendConfirmationResponse(BaseModel):
    """Output of `POST /api/v1/pedidos/{pedidoId}/reenviar-confirmacion` (`SPEC-16` Req. 3)."""

    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    pedidoId: str
    reenviosRestantes: int = Field(..., ge=0, le=2)
    correoEnmascarado: str
