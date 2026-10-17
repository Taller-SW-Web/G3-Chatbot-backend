"""In-memory ToolExecutorPort for tests of the agent loop."""
from collections.abc import Callable, Mapping
from typing import Any

from src.ports.outbound.llm_provider_port import ToolDefinition
from src.ports.outbound.tool_executor_port import ToolExecutionContext, ToolResult


class FakeToolExecutor:
    """`results` maps a tool name to a ToolResult, or to a callable(arguments) returning one (or raising)."""

    def __init__(
        self,
        results: Mapping[str, ToolResult | Callable[[Mapping[str, Any]], ToolResult]] | None = None,
        definitions: list[ToolDefinition] | None = None,
    ):
        self._results = dict(results or {})
        self._definitions = definitions or [
            ToolDefinition(name=name, description=f"fake {name}", parameters={"type": "object"})
            for name in self._results
        ]
        self.executed: list[tuple[str, dict[str, Any], ToolExecutionContext]] = []

    def definitions(self) -> list[ToolDefinition]:
        return list(self._definitions)

    async def execute(
        self, name: str, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        self.executed.append((name, dict(arguments), context))
        result = self._results.get(name)
        if result is None:
            return ToolResult(code="HERRAMIENTA_DESCONOCIDA", is_error=True)
        return result(arguments) if callable(result) else result
