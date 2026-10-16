"""ToolExecutorPort over the ToolRegistry of the tools module (SPEC-05 Req. 3 and 6).

The registry speaks Spanish and carries UI blocks inside `data["bloques"]`; the port is
provider-neutral and carries blocks next to the data. This adapter translates between both and
leaves handler exceptions untouched: the agent loop maps them to ERROR_INTERNO.
"""
from collections.abc import Mapping
from typing import Any

from src.application.tools.tool_registry import ExecutionContext, ToolRegistry
from src.ports.outbound.llm_provider_port import ToolDefinition
from src.ports.outbound.tool_executor_port import ToolExecutionContext, ToolResult

# Codes the registry returns on its own, before any handler runs. They are fed back to the model as errors.
_REGISTRY_REJECTIONS = frozenset(
    {"HERRAMIENTA_DESCONOCIDA", "ARGUMENTOS_INVALIDOS", "REQUIERE_SESION", "REQUIERE_CONFIRMACION"}
)
_BLOCKS_KEY = "bloques"


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
        data = dict(result.data)
        raw_blocks = data.pop(_BLOCKS_KEY, ())  # UI payloads never reach the model
        blocks = tuple(dict(b) for b in raw_blocks if isinstance(b, Mapping)) if isinstance(raw_blocks, list) else ()
        return ToolResult(
            code=result.code, data=data, is_error=result.code in _REGISTRY_REJECTIONS, blocks=blocks
        )


def _to_registry_context(context: ToolExecutionContext) -> ExecutionContext:
    return ExecutionContext(
        conversacion_id=str(context.conversation_id),
        cliente_id=str(context.customer_id) if context.customer_id is not None else None,
        chat_sid=context.anonymous_sid,
    )
