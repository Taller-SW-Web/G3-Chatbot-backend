"""Esquemas y registro de las herramientas del catálogo comercial.

Los handlers de catálogo/carrito se inyectan desde sus casos de uso. Resolver
referencias, normalizar filtros, validar stock y pertenencia es tarea de ellos.
"""

from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.application.tools.tool_registry import (
    ExecutionContext, ToolDefinition, ToolHandler, ToolRegistry, ToolResult,
)


Text = Annotated[str, Field(min_length=1, max_length=1000)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class EmptyArguments(ToolArguments):
    pass


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
    orden: Literal["PRECIO_ASC", "PRECIO_DESC", "RELEVANCIA", "NOVEDAD"] | None = None
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


class ProductArguments(ToolArguments):
    productoRef: Annotated[int, Field(strict=True, ge=1, le=10)] | None = None
    product_id: Text | None = None

    @model_validator(mode="after")
    def validar_referencia(self) -> Self:
        if (self.productoRef is None) == (self.product_id is None):
            raise ValueError("Indica una referencia de producto")
        return self


class AvailabilityArguments(ProductArguments):
    talla: Text | None = None
    color: Text | None = None


class AddArguments(ToolArguments):
    sku: Text | None = None
    productoRef: Annotated[int, Field(strict=True, ge=1, le=10)] | None = None
    product_id: Text | None = None
    talla: Text | None = None
    color: Text | None = None
    cantidad: Annotated[int, Field(strict=True, ge=1, le=10)] = 1

    @model_validator(mode="after")
    def validar_referencia(self) -> Self:
        if sum(value is not None for value in (self.sku, self.productoRef, self.product_id)) != 1:
            raise ValueError("Indica una referencia de producto o SKU")
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
    return ToolResult("OK", {
        "texto": "Puedes crear tu cuenta completando este formulario.",
        "bloques": [{"tipo": "FORMULARIO", "formulario": "REGISTRO"}],
    })


_TOOLS = (
    ("buscar_productos", "Buscar productos activos con filtros para el canal Chatbot.", SearchArguments),
    ("ver_detalle_producto", "Consultar el detalle de un producto o referencia del carrusel.", ProductArguments),
    ("consultar_disponibilidad", "Consultar disponibilidad de un producto o variante.", AvailabilityArguments),
    ("agregar_al_carrito", "Agregar un SKU o resolver un producto y su variante al carrito.", AddArguments),
    ("ver_carrito", "Ver el carrito con precios y totales recalculados.", EmptyArguments),
    ("cambiar_cantidad", "Cambiar cantidad de una línea; cero elimina la línea.", QuantityArguments),
    ("quitar_del_carrito", "Quitar una línea del carrito.", ItemArguments),
    ("vaciar_carrito", "Vaciar el carrito tras confirmación explícita en la UI.", EmptyArguments),
)


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
            requiere_confirmacion=nombre == "vaciar_carrito",
        ))
    return registry
