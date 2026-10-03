"""Acciones REST de la UI hacia los mismos handlers (SPEC-05, requisito 8)."""

from collections.abc import Mapping
from typing import Any

from src.application.tools.tool_registry import ExecutionContext, ToolRegistry, ToolResult


H3_ACTIONS = {
    "SOLICITAR_REGISTRO": "solicitar_registro",
    "BUSCAR_PRODUCTOS": "buscar_productos",
    "BUSCAR_PAGINA": "buscar_productos",
    "VER_DETALLE_PRODUCTO": "ver_detalle_producto",
    "CONSULTAR_DISPONIBILIDAD": "consultar_disponibilidad",
    "AGREGAR_AL_CARRITO": "agregar_al_carrito",
    "VER_CARRITO": "ver_carrito",
    "CAMBIAR_CANTIDAD": "cambiar_cantidad",
    "QUITAR_DEL_CARRITO": "quitar_del_carrito",
    "VACIAR_CARRITO": "vaciar_carrito",
}


class ActionDispatcher:
    def __init__(
        self, registry: ToolRegistry, acciones: Mapping[str, str] | None = None,
    ) -> None:
        self._registry = registry
        self._acciones = dict(H3_ACTIONS if acciones is None else acciones)

    async def despachar(
        self, accion: Mapping[str, Any], contexto: ExecutionContext,
        *, confirmacion_ui: bool = False,
    ) -> ToolResult:
        """El llamador registra el historial y controla la transacción.

        confirmacion_ui se habilita tras validar en el servidor la acción
        pendiente y su pertenencia. No se lee confirmación desde accion/payload.
        """
        if not isinstance(accion, Mapping) or set(accion) - {"tipo", "payload"}:
            return ToolResult("ACCION_INVALIDA")
        tipo = accion.get("tipo")
        if not isinstance(tipo, str) or tipo not in self._acciones:
            return ToolResult("ACCION_DESCONOCIDA")
        payload = accion.get("payload", {})
        if not isinstance(payload, Mapping):
            return ToolResult("ACCION_INVALIDA")
        return await self._registry.ejecutar_accion(
            self._acciones[tipo], payload, contexto, confirmacion_ui=confirmacion_ui,
        )
