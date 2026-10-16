"""Outbound port: tools the agent loop can offer to the LLM and execute (SPEC-05 Req. 3 and 6).

Owned by the conversation engine. The ToolRegistry/ActionDispatcher of the tools module implement it
through a thin adapter; no SDK or registry import here.
"""
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.ports.outbound.llm_provider_port import ToolDefinition

INVALID_ARGUMENTS = "ARGUMENTOS_INVALIDOS"  # code shared with the registry contract (SPEC-05 Req. 6)


@dataclass(frozen=True)
class ToolExecutionContext:
    """Built by the server from the validated caller identity, never from LLM arguments."""

    conversation_id: uuid.UUID
    customer_id: uuid.UUID | None
    anonymous_sid: str | None


@dataclass(frozen=True)
class ToolResult:
    """Outcome of one tool call. `is_error` results are fed back to the model so it can recover."""

    code: str
    data: Mapping[str, Any] = field(default_factory=dict)
    is_error: bool = False
    blocks: Sequence[Mapping[str, Any]] = ()  # structured blocks for the client (e.g. a product carousel)


class ToolExecutorPort(Protocol):
    def definitions(self) -> list[ToolDefinition]:
        """Tools available to this caller context, in the provider-neutral shape."""
        ...

    async def execute(
        self, name: str, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        """Validate and run one tool call. Domain failures come back as `is_error` results;
        only unexpected failures raise."""
        ...
