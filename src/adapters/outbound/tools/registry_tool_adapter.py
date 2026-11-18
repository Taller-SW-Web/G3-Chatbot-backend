"""ToolExecutorPort over the ToolRegistry of the tools module (SPEC-05 Req. 3 and 6).

The registry speaks Spanish and carries UI blocks inside `data["bloques"]`; the port is
provider-neutral and carries blocks next to the data. This adapter translates between both and
leaves handler exceptions untouched: the agent loop maps them to ERROR_INTERNO.
"""
import uuid
from collections.abc import Mapping
from typing import Any

from src.application.tools.tool_registry import ExecutionContext, ToolRegistry
from src.ports.outbound.llm_provider_port import ToolDefinition
from src.ports.outbound.tool_executor_port import ToolExecutionContext, ToolResult

_CONFIRMATION_REQUIRED = "REQUIERE_CONFIRMACION"
# Codes the registry returns on its own, before any handler runs. They are fed back to the model as errors.
_REGISTRY_REJECTIONS = frozenset(
    {"HERRAMIENTA_DESCONOCIDA", "ARGUMENTOS_INVALIDOS", "REQUIERE_SESION", _CONFIRMATION_REQUIRED}
)
_BLOCKS_KEY = "bloques"

# Tools that need an explicit on-screen click. Confirm re-sends the action with
# `confirmado: true`; only that UI path (never the LLM) may execute it.
_CONFIRMATION_PROMPTS: dict[str, dict[str, Any]] = {
    "vaciar_carrito": {
        "pregunta": "¿Seguro que quieres vaciar tu carrito?",
        "confirmar": {"id": "VACIAR_CARRITO", "etiqueta": "Sí, vaciar", "payload": {"confirmado": True}},
        "cancelar": {"id": "VER_CARRITO", "etiqueta": "Cancelar", "payload": {}},
    },
}


class RegistryToolAdapter:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def definitions(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(name=tool["nombre"], description=tool["descripcion"], parameters=tool["parametros"])
            for tool in self._registry.catalogo()
        ]

    async def execute(
        self, name: str, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        result = await self._registry.ejecutar(name, arguments, _to_registry_context(context))
        if result.code == _CONFIRMATION_REQUIRED:
            prompt = _confirmation_prompt(result.data)
            if prompt is not None:
                return prompt
        data = dict(result.data)
        raw_blocks = data.pop(_BLOCKS_KEY, ())  # UI payloads never reach the model
        blocks = tuple(_with_id(b) for b in raw_blocks if isinstance(b, Mapping)) if isinstance(raw_blocks, list) else ()
        return ToolResult(
            code=result.code, data=data, is_error=result.code in _REGISTRY_REJECTIONS, blocks=blocks
        )


def _confirmation_prompt(data: Mapping[str, Any]) -> ToolResult | None:
    """Turn a pending confirmation into buttons the customer can click."""
    pending = data.get("accionPendiente")
    tool = pending.get("herramienta") if isinstance(pending, Mapping) else None
    prompt = _CONFIRMATION_PROMPTS.get(tool) if isinstance(tool, str) else None
    if prompt is None:
        return None
    actions = [dict(prompt["confirmar"]), dict(prompt["cancelar"])]
    block = {
        "id": str(uuid.uuid4()),
        "tipo": "ACCIONES_RAPIDAS",
        "pregunta": prompt["pregunta"],
        "acciones": actions,
        "datos": {"pregunta": prompt["pregunta"], "acciones": actions},
    }
    return ToolResult(
        code=_CONFIRMATION_REQUIRED,
        data={"texto": "Se mostró al cliente la confirmación con botones en pantalla. "
                       "No ejecutes la acción ni la pidas de nuevo; espera su respuesta."},
        is_error=False,
        blocks=(block,),
    )


def _with_id(block: Mapping[str, Any]) -> dict[str, Any]:
    """Every block the client renders needs an id; handlers may omit it, so one is assigned here."""
    return {"id": str(uuid.uuid4()), **block} if not block.get("id") else dict(block)


def _to_registry_context(context: ToolExecutionContext) -> ExecutionContext:
    return ExecutionContext(
        conversacion_id=str(context.conversation_id),
        cliente_id=str(context.customer_id) if context.customer_id is not None else None,
        chat_sid=context.anonymous_sid,
    )
