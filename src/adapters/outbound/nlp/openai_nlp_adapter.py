"""OpenAI implementation of LLMProvider (ADR-0005) over the Responses API with streaming and tool calling."""
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import openai
from openai import AsyncOpenAI

from src.ports.outbound.llm_provider_port import (
    Completed,
    FinishReason,
    ImagePart,
    LLMEvent,
    LLMMessage,
    LLMRole,
    LLMTimeoutError,
    LLMUnavailableError,
    TextDelta,
    TextPart,
    TokenUsage,
    ToolCall,
    ToolCallRequested,
    ToolDefinition,
)

OPENAI_TEMPERATURE_RANGE = (0.0, 2.0)
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high")
# Why a response ended incomplete -> neutral finish reason (a truncated answer is not an error).
_INCOMPLETE_REASONS: dict[str, FinishReason] = {
    "max_output_tokens": "length",
    "content_filter": "content_filter",
}


class OpenAILLMProvider:
    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        temperature: float,
        max_tokens: int,
        timeout_seconds: float,
        vision_enabled: bool = True,
        reasoning_effort: str = "none",
        client: AsyncOpenAI | None = None,
    ):
        low, high = OPENAI_TEMPERATURE_RANGE
        if not low <= temperature <= high:
            raise ValueError(f"LLM_TEMPERATURE must be between {low} and {high} for OpenAI")
        if reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                f"LLM_REASONING_EFFORT must be one of {', '.join(REASONING_EFFORTS)}, got {reasoning_effort!r}"
            )
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout_seconds = timeout_seconds
        self._vision_enabled = vision_enabled
        self._reasoning_effort = reasoning_effort
        self._client = client

    @property
    def supports_vision(self) -> bool:
        return self._vision_enabled

    def _get_client(self) -> AsyncOpenAI:
        # Built on first use so a missing key never prevents the app from starting.
        if self._client is None:
            if not self._api_key:
                raise LLMUnavailableError("LLM_API_KEY is not configured")
            # max_retries=0: the SDK would otherwise retry timeouts and multiply the configured timeout.
            self._client = AsyncOpenAI(
                api_key=self._api_key, timeout=self._timeout_seconds, max_retries=0
            )
        return self._client

    async def stream(
        self, messages: Sequence[LLMMessage], tools: Sequence[ToolDefinition]
    ) -> AsyncIterator[LLMEvent]:
        client = self._get_client()
        request: dict[str, Any] = {
            "model": self._model,
            "input": self._to_input_items(messages),
            "max_output_tokens": self._max_tokens,
            "reasoning": {"effort": self._reasoning_effort},
            "store": False,  # never persist conversations at OpenAI
            "stream": True,
        }
        # Reasoning models only accept the default temperature; it is sent just when reasoning is off.
        if self._reasoning_effort == "none":
            request["temperature"] = self._temperature
        if tools:
            request["tools"] = [self._to_openai_tool(t) for t in tools]

        pending: dict[int, dict[str, str]] = {}  # output index -> call id, name, argument fragments
        terminal: Any = None
        try:
            response = await client.responses.create(**request)
            async for event in response:
                kind = event.type
                if kind == "response.output_text.delta":
                    if event.delta:
                        yield TextDelta(event.delta)
                elif kind == "response.output_item.added" and event.item.type == "function_call":
                    entry = pending.setdefault(event.output_index, {"id": "", "name": "", "args": ""})
                    entry["id"], entry["name"] = event.item.call_id, event.item.name
                elif kind == "response.function_call_arguments.delta":
                    entry = pending.setdefault(event.output_index, {"id": "", "name": "", "args": ""})
                    entry["args"] += event.delta
                elif kind == "response.output_item.done" and event.item.type == "function_call":
                    entry = pending.setdefault(event.output_index, {"id": "", "name": "", "args": ""})
                    # The finished item carries the authoritative call id, name and full arguments.
                    entry["id"], entry["name"] = event.item.call_id, event.item.name
                    entry["args"] = event.item.arguments
                elif kind in ("response.completed", "response.incomplete"):
                    terminal = event
                    break
                elif kind == "response.failed":
                    raise LLMUnavailableError("LLM response failed")
                elif kind == "error":
                    raise LLMUnavailableError(f"LLM stream error: {event.code or 'unknown'}")
        except openai.APITimeoutError as exc:  # before APIConnectionError, its parent
            raise LLMTimeoutError("LLM request timed out") from exc
        except openai.APIStatusError as exc:
            raise LLMUnavailableError(f"LLM provider answered HTTP {exc.status_code}") from exc
        except openai.OpenAIError as exc:
            raise LLMUnavailableError(f"LLM provider call failed: {type(exc).__name__}") from exc

        if terminal is None:
            raise LLMUnavailableError("LLM stream ended without a final response")
        for index in sorted(pending):
            yield ToolCallRequested(self._assemble_tool_call(pending[index]))
        if terminal.type == "response.incomplete":
            details = terminal.response.incomplete_details
            reason = _INCOMPLETE_REASONS.get(details.reason if details and details.reason else "", "other")
        else:
            reason = "tool_calls" if pending else "stop"
        raw_usage = terminal.response.usage
        usage = TokenUsage(raw_usage.input_tokens, raw_usage.output_tokens) if raw_usage else None
        yield Completed(reason, usage)

    @staticmethod
    def _assemble_tool_call(entry: dict[str, str]) -> ToolCall:
        try:
            arguments = json.loads(entry["args"] or "{}")
        except json.JSONDecodeError as exc:
            raise LLMUnavailableError(f"Malformed arguments for tool {entry['name']!r}") from exc
        if not isinstance(arguments, dict):
            raise LLMUnavailableError(f"Arguments for tool {entry['name']!r} are not an object")
        return ToolCall(id=entry["id"], name=entry["name"], arguments=arguments)

    def _to_input_items(self, messages: Sequence[LLMMessage]) -> list[dict[str, Any]]:
        # The system prompt stays an in-order "system" input item (not `instructions`), so the history
        # keeps exactly the order the use case built.
        items: list[dict[str, Any]] = []
        for message in messages:
            if message.role is LLMRole.USER:
                content = [self._to_input_part(p) for p in message.content]
                items.append({"role": "user", "content": content})
                continue
            text = "".join(p.text for p in message.content if isinstance(p, TextPart))
            if message.role is LLMRole.TOOL:
                items.append(
                    {"type": "function_call_output", "call_id": message.tool_call_id, "output": text}
                )
                continue
            if text:
                items.append({"role": message.role.value, "content": text})
            for call in message.tool_calls or ():
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    }
                )
        return items

    def _to_input_part(self, part: TextPart | ImagePart) -> dict[str, Any]:
        if isinstance(part, TextPart):
            return {"type": "input_text", "text": part.text}
        if not self._vision_enabled:
            raise LLMUnavailableError("Image content sent while vision is disabled")
        return {
            "type": "input_image",
            "image_url": f"data:{part.media_type};base64,{part.data_base64}",
            "detail": "auto",
        }

    @staticmethod
    def _to_openai_tool(tool: ToolDefinition) -> dict[str, Any]:
        # strict=False explicitly: the Responses API defaults to strict, which would require every property
        # to be required, while the registry schemas have optional fields.
        return {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
            "strict": False,
        }
