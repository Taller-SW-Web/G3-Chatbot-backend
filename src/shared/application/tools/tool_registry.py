"""Catálogo y ejecución segura de herramientas (SPEC-05, requisitos 3 y 6)."""

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Construido por el servidor tras validar token y pertenencia.

    Nunca construirlo desde argumentos del LLM. El caso de uso también valida
    las referencias de producto/item contra esta conversación y su carrito.
    `access_token` viaja solo para reenviar llamadas del titular (Ventas
    `GET /pedidos/me`) desde el chat; nunca se registra ni se expone al LLM.
    """

    conversacion_id: str | None = None
    cliente_id: str | None = None
    chat_sid: str | None = None
    access_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ToolResult:
    code: str
    data: Mapping[str, Any] = field(default_factory=dict)


ToolHandler = Callable[[BaseModel, ExecutionContext], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    nombre: str
    descripcion: str
    argumentos: type[BaseModel]
    handler: ToolHandler
    requiere_sesion: bool = False
    requiere_confirmacion: bool = False
    # UI-only actions (CONFIRMAR_RECLAMO/DEVOLUCION) are never offered to the
    # LLM: `catalogo()` omits them and `ejecutar()` rejects them by name.
    solo_ui: bool = False
    # SPEC-03 Req. 3: protected tools may name the action to resume after login.
    accion_pendiente_sesion: str | None = None


# SPEC-05 exige ignorar la identidad propuesta por el LLM, no usarla.
_IDENTITY_KEYS = frozenset({"clienteId", "cliente_id", "sub", "token", "chat_sid"})


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def registrar(self, tool: ToolDefinition) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", tool.nombre):
            raise ValueError("Nombre de herramienta inválido")
        if tool.nombre in self._tools:
            raise ValueError(f"Herramienta duplicada: {tool.nombre}")
        if tool.argumentos.model_config.get("extra") != "forbid":
            raise ValueError("El esquema de herramienta debe prohibir argumentos extra")
        if not callable(tool.handler):
            raise ValueError("Falta el handler de la herramienta")
        self._tools[tool.nombre] = tool

    def catalogo(self) -> list[dict[str, Any]]:
        """Formato neutral para que el adapter LLM lo traduzca a su proveedor."""
        return [
            {
                "nombre": tool.nombre,
                "descripcion": tool.descripcion,
                "parametros": tool.argumentos.model_json_schema(),
                "requiere_sesion": tool.requiere_sesion,
                "requiere_confirmacion": tool.requiere_confirmacion,
            }
            for tool in self._tools.values()
            if not tool.solo_ui
        ]

    def contiene(self, nombre: str, *, incluir_solo_ui: bool = False) -> bool:
        """Whether the registry can dispatch `nombre` (UI path may include hidden actions)."""
        tool = self._tools.get(nombre)
        return tool is not None and (incluir_solo_ui or not tool.solo_ui)

    async def ejecutar(
        self, nombre: str, argumentos: Mapping[str, Any] | str,
        contexto: ExecutionContext,
    ) -> ToolResult:
        """Camino LLM: nunca autoriza acciones que requieran confirmación."""
        return await self._ejecutar(
            nombre, argumentos, contexto, confirmada=False, permitir_solo_ui=False,
        )

    async def ejecutar_accion(
        self, nombre: str, argumentos: Mapping[str, Any],
        contexto: ExecutionContext, *, confirmacion_ui: bool = False,
    ) -> ToolResult:
        """Camino UI. El router valida la confirmación pendiente antes de usarlo.

        confirmacion_ui es un parámetro del servidor, nunca extraído del payload
        de la herramienta ni de la salida del LLM.
        """
        return await self._ejecutar(
            nombre, argumentos, contexto, confirmada=confirmacion_ui is True,
            permitir_solo_ui=True,
        )

    async def _ejecutar(
        self, nombre: str, argumentos: Mapping[str, Any] | str,
        contexto: ExecutionContext, *, confirmada: bool, permitir_solo_ui: bool,
    ) -> ToolResult:
        tool = self._tools.get(nombre)
        if tool is None or (tool.solo_ui and not permitir_solo_ui):
            return ToolResult("HERRAMIENTA_DESCONOCIDA")
        if isinstance(argumentos, str):
            try:
                argumentos = json.loads(argumentos)
            except (ValueError, RecursionError):
                return ToolResult("ARGUMENTOS_INVALIDOS")
        if not isinstance(argumentos, Mapping):
            return ToolResult("ARGUMENTOS_INVALIDOS")
        limpios = {k: v for k, v in argumentos.items() if k not in _IDENTITY_KEYS}
        try:
            validados = tool.argumentos.model_validate(limpios)
        except ValidationError as error:
            # No enviar input ni mensajes personalizados que puedan contener PII.
            campos = tool.argumentos.model_fields
            errores = [
                {
                    "campo": str(e["loc"][0])
                    if e["loc"] and e["loc"][0] in campos else "argumentos",
                    "tipo": e["type"],
                }
                for e in error.errors(include_input=False, include_context=False)
            ]
            return ToolResult("ARGUMENTOS_INVALIDOS", {"errores": errores})
        pendiente = {"herramienta": nombre, "argumentos": validados.model_dump(mode="json")}
        if tool.requiere_sesion and not contexto.cliente_id:
            if tool.accion_pendiente_sesion:
                return ToolResult(
                    "REQUIERE_SESION", {"accionPendiente": tool.accion_pendiente_sesion},
                )
            return ToolResult("REQUIERE_SESION", {"accionPendiente": pendiente})
        if tool.requiere_confirmacion and not confirmada:
            return ToolResult("REQUIERE_CONFIRMACION", {"accionPendiente": pendiente})
        # No reintentar ni capturar errores de dominio/transacción aquí.
        return await tool.handler(validados, contexto)
