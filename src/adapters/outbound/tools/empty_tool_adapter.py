"""TEMPORARY ToolExecutorPort with no tools, until the ToolRegistry of the tools module is merged.

With no tools the assistant can only chat; every tool call it attempts is answered as unknown.
"""
from collections.abc import Mapping
from typing import Any

from src.ports.outbound.llm_provider_port import ToolDefinition
from src.ports.outbound.tool_executor_port import ToolExecutionContext, ToolResult


class EmptyToolAdapter:
    def definitions(self) -> list[ToolDefinition]:
        return []

    async def execute(
        self, name: str, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        return ToolResult(code="HERRAMIENTA_DESCONOCIDA", is_error=True)
