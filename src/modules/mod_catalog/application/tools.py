"""Esquemas y registro de las herramientas del catálogo comercial.

Los handlers de catálogo/carrito se inyectan desde sus casos de uso. Resolver
referencias, normalizar filtros, validar stock y pertenencia es tarea de ellos.
"""

from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.shared.application.tools.tool_registry import (
    ExecutionContext,
    ToolDefinition,
    ToolHandler,
    ToolRegistry,
    ToolResult,
)

Text = Annotated[str, Field(min_length=1, max_length=1000)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class EmptyArguments(ToolArguments):
    pass


class PromotionArguments(ToolArguments):
    categoria: Text | None = None
    marca: Text | None = None


class RecommendationArguments(ToolArguments):
    actividad: Text
    uso: Text | None = None
    nivel: Text | None = None
    destinatario: Text | None = None
    talla: Text | None = None
    edad: Annotated[int, Field(strict=True, ge=0, le=120)] | None = None
    superficie: Text | None = None
    presupuestoMax: Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)] | None = None
    marcaPreferida: Text | None = None
    omitirPreguntas: bool = Field(default=False, strict=True)


class SearchArguments(ToolArguments):
    q: Text | None = None
    categoria: Text | None = None
    marca: Text | None = None
    categoriaId: Text | None = None
    marcaId: Text | None = None
    uso: Text | None = None
    talla: Text | None = None
    color: Text | None = None
    soloOfertas: bool | None = Field(default=None, strict=True)
    orden: Literal[
        "PRECIO_ASC", "PRECIO_DESC", "RELEVANCIA", "NOVEDAD",
        "NOMBRE_ASC", "NOMBRE_DESC",
        "relevancia", "precio_asc", "precio_desc", "nombre_asc", "nombre_desc",
    ] | None = None
    precioMin: Money | None = None
    precioMax: Money | None = None
    pagina: PositiveInt = 1
    tamanio: Annotated[int, Field(strict=True, ge=1, le=10)] = 10
    canal: Literal["CHATBOT"] = "CHATBOT"
    estado: Literal["ACTIVO"] = "ACTIVO"

    @model_validator(mode="after")
    def validar_rango(self) -> Self:
        if self.precioMin is not None and self.precioMax is not None:
            if self.precioMin > self.precioMax:
                raise ValueError("Rango de precios inválido")
        return self


class OrderQueryArguments(ToolArguments):
    """SPEC-17: one order by number (`pedidoId`), or the customer's list by state."""

    pedidoId: Text | None = None
    estado: Literal[
        "CREADO", "PAGADO", "EN_PREPARACION", "DESPACHADO", "ENTREGADO", "ANULADO"
    ] | None = None


class TrackingArguments(ToolArguments):
    """SPEC-18: tracking by opaque `pedidoId` or by the customer-spoken `pedidoRef`."""

    pedidoId: Text | None = None
    pedidoRef: Text | None = None

    @model_validator(mode="after")
    def validar_referencia(self) -> Self:
        if sum(value is not None for value in (self.pedidoId, self.pedidoRef)) != 1:
            raise ValueError("Indica una referencia de pedido")
        return self


class CouponArguments(ToolArguments):
    """SPEC-13: coupon code plus the explicit UI confirmation to replace another one."""

    codigo: Text
    confirmarReemplazo: bool = Field(default=False, strict=True)


class ConversationSearchArguments(ToolArguments):
    q: Annotated[str, Field(min_length=1, max_length=100)]


class ClaimLookupArguments(ToolArguments):
    codigoSeguimiento: Text


class ReturnLookupArguments(ToolArguments):
    devolucionId: Text


ClaimReason = Literal[
    "INCUMPLIMIENTO_PLAZO_ENTREGA",
    "PRODUCTO_DEFECTUOSO_O_INCORRECTO",
    "COBRO_INDEBIDO_O_NO_RECONOCIDO",
    "ATENCION_INADECUADA",
    "INCUMPLIMIENTO_GARANTIA",
]
ClaimType = Literal["RECLAMO", "QUEJA"]
ReturnType = Literal["CAMBIO", "DEVOLUCION_DINERO"]
ReturnReason = Literal[
    "TALLA_INCORRECTA",
    "NO_ERA_LO_QUE_ESPERABA",
    "PRODUCTO_DEFECTUOSO",
    "PRODUCTO_EQUIVOCADO_ENVIADO",
    "YA_NO_LO_QUIERO",
    "OTRO",
]


class ClaimPrepareArguments(ToolArguments):
    """SPEC-19: prefill the claim form; nothing is registered until the button is pressed."""

    pedidoRef: Text | None = None
    tipo: ClaimType | None = None
    motivo: ClaimReason | None = None
    descripcion: Annotated[str, Field(max_length=1000)] | None = None
    pedidoCliente: Text | None = None


class ClaimConsumerArguments(ToolArguments):
    nombreCompleto: Annotated[str, Field(min_length=1, max_length=150)]
    documento: Annotated[str, Field(min_length=1, max_length=20)]
    email: Annotated[str, Field(min_length=3, max_length=150)]
    telefono: Annotated[str, Field(min_length=1, max_length=20)]


class ClaimConfirmArguments(ToolArguments):
    """SPEC-19 Req. 2: the claim form submit (UI action `CONFIRMAR_RECLAMO`)."""

    pedidoId: Text
    tipo: ClaimType
    motivo: ClaimReason
    descripcion: Annotated[str, Field(min_length=20, max_length=1000)]
    consumidor: ClaimConsumerArguments
    solucionEsperada: Annotated[str, Field(max_length=500)] | None = None
    forzar: bool = Field(default=False, strict=True)
    idempotencyKey: Annotated[str, Field(min_length=8, max_length=120)] | None = None


class ReturnPrepareArguments(ToolArguments):
    """SPEC-21: prefill the return/change form; nothing is registered until the button is pressed."""

    pedidoRef: Text | None = None
    lineaRef: Text | None = None
    tipo: ReturnType | None = None
    motivo: ReturnReason | None = None
    varianteDeseada: Text | None = None
    descripcion: Annotated[str, Field(max_length=1000)] | None = None


class ReturnItemArguments(ToolArguments):
    productoId: Text
    cantidad: Annotated[int, Field(strict=True, ge=1, le=99)]


class ReturnEvidenceArguments(ToolArguments):
    tipo: Literal["IMAGEN", "DOCUMENTO"]
    url: Annotated[str, Field(min_length=1, max_length=500)]


class ReturnConfirmArguments(ToolArguments):
    """SPEC-21 Req. 4: the return form submit (UI action `CONFIRMAR_DEVOLUCION`)."""

    pedidoId: Text
    tipo: ReturnType
    motivo: ReturnReason
    descripcion: Annotated[str, Field(max_length=1000)] | None = None
    items: list[ReturnItemArguments] = Field(min_length=1)
    evidencias: list[ReturnEvidenceArguments] = Field(default_factory=list, max_length=3)
    forzar: bool = Field(default=False, strict=True)
    idempotencyKey: Annotated[str, Field(min_length=8, max_length=120)] | None = None


class ProductArguments(ToolArguments):
    productoRef: Annotated[int, Field(strict=True, ge=1, le=10)] | None = None
    product_id: Text | None = None
    nombre: Text | None = None

    @model_validator(mode="after")
    def validar_referencia(self) -> Self:
        if sum(value is not None for value in (self.productoRef, self.product_id, self.nombre)) != 1:
            raise ValueError("Indica una referencia de producto")
        return self


class AvailabilityArguments(ProductArguments):
    talla: Text | None = None
    color: Text | None = None


class AddArguments(ToolArguments):
    sku: Text | None = None
    productoRef: Annotated[int, Field(strict=True, ge=1, le=10)] | None = None
    product_id: Text | None = None
    nombre: Text | None = None
    talla: Text | None = None
    color: Text | None = None
    cantidad: Annotated[int, Field(strict=True, ge=1, le=10)] = 1

    @model_validator(mode="after")
    def validar_referencia(self) -> Self:
        # An explicit product supplies verified merchandising context for a SKU.
        # Carousel references and names are mutually exclusive with every other reference.
        has_ids = self.sku is not None or self.product_id is not None
        exclusive = (self.productoRef is not None) + (self.nombre is not None) + has_ids
        if exclusive != 1:
            raise ValueError("Indica una referencia de producto, nombre o SKU")
        return self


class ItemArguments(ToolArguments):
    itemId: Text | None = None
    itemRef: Annotated[int, Field(strict=True, ge=1, le=20)] | None = None
    nombre: Text | None = None

    @model_validator(mode="after")
    def validar_referencia(self) -> Self:
        if sum(value is not None for value in (self.itemId, self.itemRef, self.nombre)) != 1:
            raise ValueError("Indica una referencia de línea")
        return self


class QuantityArguments(ItemArguments):
    cantidad: Annotated[int, Field(strict=True, ge=0, le=10)]


async def solicitar_registro(
    argumentos: BaseModel, contexto: ExecutionContext,
) -> ToolResult:
    """Abre el formulario; no recoge datos ni crea una cuenta en Seguridad."""
    if contexto.cliente_id:
        # The UI renders nothing for a logged-in customer; answer in text instead.
        return ToolResult("OK", {"texto": "Ya tienes una sesión iniciada con tu cuenta."})
    return ToolResult("OK", {
        "texto": "Puedes crear tu cuenta completando este formulario.",
        "bloques": [{"tipo": "FORMULARIO", "formulario": "REGISTRO"}],
    })


_TOOLS = (
    ("consultar_promociones", "Consultar promociones y productos en oferta sin revelar cupones.", PromotionArguments),
    ("recomendar_productos", "Seleccionar productos por actividad y necesidad; razones verificadas por servidor.", RecommendationArguments),
    ("silenciar_complementos", "No sugerir más complementos durante esta conversación.", EmptyArguments),
    ("buscar_productos", (
        "Buscar productos activos. `q` lleva SOLO palabras clave del producto (tipo, uso, modelo; "
        "ej. \"zapatillas running\"); nunca precios, monedas ni frases como \"menos de\". "
        "El precio va en `precioMin`/`precioMax` (soles) y la marca en `marca`."
    ), SearchArguments),
    ("ver_detalle_producto", (
        "Consultar el detalle de un producto. Indica exactamente una referencia: `productoRef` "
        "(número del carrusel), `product_id` o `nombre` (nombre del producto mencionado por el cliente)."
    ), ProductArguments),
    ("consultar_disponibilidad", (
        "Consultar disponibilidad de un producto o variante (talla, color). Identifica el producto con "
        "`productoRef` (carrusel), `product_id` o `nombre`; no necesita un carrusel previo."
    ), AvailabilityArguments),
    ("agregar_al_carrito", (
        "Agregar un producto al carrito. Identifícalo con `productoRef` (carrusel), `product_id`, `sku` o `nombre` "
        "(úsalo cuando el cliente nombra el producto; no adivines `productoRef` ni `sku`). "
        "Si faltan talla o color, el servidor los pide al cliente."
    ), AddArguments),
    ("ver_carrito", "Ver el carrito con precios y totales recalculados.", EmptyArguments),
    ("cambiar_cantidad", "Cambiar cantidad de una línea; cero elimina la línea.", QuantityArguments),
    ("quitar_del_carrito", "Quitar una línea del carrito.", ItemArguments),
    ("vaciar_carrito", "Vaciar el carrito tras confirmación explícita en la UI.", EmptyArguments),
    ("aplicar_cupon", (
        "Aplicar un cupón al carrito del cliente con sesión iniciada. Si ya hay otro cupón, "
        "pide confirmación y vuelve a llamar con `confirmarReemplazo=true`."
    ), CouponArguments),
    ("quitar_cupon", "Quitar el cupón del carrito del cliente con sesión iniciada.", EmptyArguments),
    ("iniciar_checkout", (
        "Llevar al cliente a la pantalla de checkout; no crea el pedido ni procesa el pago, "
        "que solo ocurren en esa pantalla. Requiere sesión iniciada."
    ), EmptyArguments),
    ("consultar_pedidos", (
        "Consultar el estado de los pedidos del cliente con sesión iniciada. Sin argumentos lista "
        "sus pedidos recientes o muestra el único pedido en curso; con `pedidoId` muestra ese "
        "pedido, su estado y su línea de tiempo."
    ), OrderQueryArguments),
    ("consultar_seguimiento", (
        "Consultar el seguimiento del despacho de un pedido del cliente con sesión iniciada. "
        "Indica `pedidoId` o `pedidoRef` con el número que dio el cliente."
    ), TrackingArguments),
    ("preparar_reclamo", (
        "Preparar el formulario de reclamo o queja de un pedido. No registra nada: el cliente "
        "lo revisa y confirma con el botón \"Enviar reclamo\". Si falta el pedido, devuelve la "
        "lista para elegir uno."
    ), ClaimPrepareArguments),
    ("listar_reclamos", (
        "Listar los reclamos del cliente con sesión iniciada, o mostrar el detalle si tiene un "
        "solo reclamo abierto."
    ), EmptyArguments),
    ("consultar_reclamo", (
        "Consultar el estado y la respuesta de un reclamo propio por `codigoSeguimiento`."
    ), ClaimLookupArguments),
    ("preparar_devolucion", (
        "Preparar el formulario de cambio o devolución de un pedido entregado. No registra "
        "nada: el cliente lo revisa y confirma con el botón \"Enviar solicitud\"."
    ), ReturnPrepareArguments),
    ("listar_devoluciones", (
        "Listar las solicitudes de cambio y devolución del cliente con sesión iniciada."
    ), EmptyArguments),
    ("consultar_devolucion", (
        "Consultar el estado de una solicitud de cambio o devolución propia por `devolucionId`."
    ), ReturnLookupArguments),
    ("solicitar_login", "Mostrar el formulario de inicio de sesión; las credenciales nunca van por el chat.", EmptyArguments),
    ("reenviar_verificacion", (
        "Mostrar el formulario para reenviar el correo de verificación de la cuenta."
    ), EmptyArguments),
    ("cerrar_sesion", (
        "Cerrar la sesión del cliente. Devuelve la acción CERRAR_SESION y el frontend ejecuta el "
        "logout contra el BFF; el backend no toca cookies."
    ), EmptyArguments),
    ("nueva_conversacion", (
        "Ofrecer empezar una conversación nueva. Devuelve la acción NUEVO_CHAT; el frontend crea "
        "la conversación."
    ), EmptyArguments),
    ("buscar_conversaciones", (
        "Ofrecer buscar en conversaciones anteriores. Devuelve la acción BUSCAR_CHATS con el "
        "término en `payload.q`; el frontend abre la búsqueda."
    ), ConversationSearchArguments),
    ("verificar_celular", (
        "Consultar o iniciar la verificación del celular del cliente. Sin argumentos devuelve si el "
        "celular vigente ya está verificado o el formulario OTP_CELULAR para verificarlo."
    ), EmptyArguments),
)


_SESSION_TOOLS = frozenset({
    "aplicar_cupon",
    "cerrar_sesion",
    "consultar_devolucion",
    "consultar_pedidos",
    "consultar_reclamo",
    "consultar_seguimiento",
    "iniciar_checkout",
    "listar_devoluciones",
    "listar_reclamos",
    "preparar_devolucion",
    "preparar_reclamo",
    "quitar_cupon",
    "verificar_celular",
})


def build_tool_registry(handlers: Mapping[str, ToolHandler] | None = None) -> ToolRegistry:
    """Registra `solicitar_registro` siempre y solo los handlers comerciales recibidos.

    Los esquemas y descripciones viven en `_TOOLS`; quien integra un módulo solo aporta
    el handler. Un nombre desconocido o un handler no invocable falla para atrapar errores
    de escritura antes de exponer una herramienta rota al LLM.
    """
    handlers = {} if handlers is None else handlers
    desconocidos = set(handlers) - {nombre for nombre, _, _ in _TOOLS}
    if desconocidos:
        raise ValueError(f"Handlers comerciales desconocidos: {sorted(desconocidos)}")
    if not all(callable(handler) for handler in handlers.values()):
        raise ValueError("Todos los handlers comerciales deben ser invocables")
    registry = ToolRegistry()
    registry.registrar(ToolDefinition(
        "solicitar_registro",
        "Mostrar el formulario seguro de registro sin pedir datos en el chat. Usar solo cuando el "
        "cliente pide crear una cuenta o registrarse; nunca para buscar productos ni para otras consultas.",
        EmptyArguments, solicitar_registro,
    ))
    for nombre, descripcion, esquema in _TOOLS:
        if nombre not in handlers:
            continue
        registry.registrar(ToolDefinition(
            nombre, descripcion, esquema, handlers[nombre],
            requiere_sesion=nombre in _SESSION_TOOLS,
            requiere_confirmacion=nombre == "vaciar_carrito",
            accion_pendiente_sesion="INICIAR_CHECKOUT" if nombre == "iniciar_checkout" else None,
        ))
    return registry
