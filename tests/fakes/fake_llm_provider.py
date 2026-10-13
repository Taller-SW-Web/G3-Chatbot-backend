"""Scriptable LLMProvider for tests of the agent loop: no SDK, no network."""
from collections.abc import AsyncIterator, Sequence

from src.ports.outbound.llm_provider_port import LLMEvent, LLMMessage, ToolDefinition


class FakeLLMProvider:
    """Each call to `stream` replays the next scripted turn (a list of events, or an exception to raise)."""

    def __init__(self, turns: Sequence[Sequence[LLMEvent] | Exception], *, supports_vision: bool = True):
        self._turns = list(turns)
        self._supports_vision = supports_vision
        self.calls: list[tuple[list[LLMMessage], list[ToolDefinition]]] = []

    @property
    def supports_vision(self) -> bool:
        return self._supports_vision

    async def stream(
        self, messages: Sequence[LLMMessage], tools: Sequence[ToolDefinition]
    ) -> AsyncIterator[LLMEvent]:
        self.calls.append((list(messages), list(tools)))
        turn = self._turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        for event in turn:
            yield event
