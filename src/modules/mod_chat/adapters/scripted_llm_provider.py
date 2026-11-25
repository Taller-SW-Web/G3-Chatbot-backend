"""Deterministic LLMProvider for load tests (`LLM_PROVIDER=fake`, H5-P2): no SDK, no network.

It exercises the real agent loop without paying for or waiting on the model: the
first call of a turn asks for `buscar_productos` with the customer's text, and the
call that receives the tool result streams a short closing text. Prices and
products always come from the tool, never from this adapter (ADR-0005).
Refused in production by `provide_llm_provider`.
"""
from collections.abc import AsyncIterator, Sequence

from src.modules.mod_chat.ports.llm_provider_port import (
    Completed,
    LLMEvent,
    LLMMessage,
    LLMRole,
    TextDelta,
    TextPart,
    TokenUsage,
    ToolCall,
    ToolCallRequested,
    ToolDefinition,
)

SEARCH_TOOL = "buscar_productos"
CLOSING_TEXT = ("Estas son algunas opciones ", "que encontré en el catálogo. ", "¿Quieres ver el detalle de alguna?")
FALLBACK_TEXT = ("Puedo ayudarte a buscar productos, ", "revisar tu carrito o tus pedidos.")
QUERY_MAX_CHARS = 120


class ScriptedLLMProvider:
    @property
    def supports_vision(self) -> bool:
        return False

    async def stream(
        self, messages: Sequence[LLMMessage], tools: Sequence[ToolDefinition]
    ) -> AsyncIterator[LLMEvent]:
        last = messages[-1] if messages else None
        if last is not None and last.role is LLMRole.USER and any(t.name == SEARCH_TOOL for t in tools):
            query = _text(last)[:QUERY_MAX_CHARS] or "zapatillas"
            call_id = f"call_{len(messages)}"
            yield ToolCallRequested(ToolCall(id=call_id, name=SEARCH_TOOL, arguments={"q": query}))
            yield Completed("tool_calls", TokenUsage(prompt_tokens=0, completion_tokens=0))
            return
        chunks = CLOSING_TEXT if last is not None and last.role is LLMRole.TOOL else FALLBACK_TEXT
        for chunk in chunks:
            yield TextDelta(chunk)
        yield Completed("stop", TokenUsage(prompt_tokens=0, completion_tokens=0))


def _text(message: LLMMessage) -> str:
    return " ".join(part.text for part in message.content if isinstance(part, TextPart)).strip()
