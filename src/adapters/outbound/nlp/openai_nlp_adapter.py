"""OpenAI implementation of LLMProvider (ADR-0005) over Chat Completions with streaming and tool calling."""
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
_FINISH_REASONS: dict[str, FinishReason] = {
    "stop": "stop",
    "tool_calls": "tool_calls",
    "length": "length",
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
        client: AsyncOpenAI | None = None,
    ):
        low, high = OPENAI_TEMPERATURE_RANGE
        if not low <= temperature <= high:
            raise ValueError(f"LLM_TEMPERATURE must be between {low} and {high} for OpenAI")
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout_seconds = timeout_seconds
        self._vision_enabled = vision_enabled
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
            "messages": [self._to_openai_message(m) for m in messages],
            "temperature": self._temperature,
            "max_completion_tokens": self._max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            request["tools"] = [self._to_openai_tool(t) for t in tools]

        pending: dict[int, dict[str, str]] = {}  # tool call index -> id, name, argument fragments
        finish_reason: str | None = None
        usage: TokenUsage | None = None
        try:
            response = await client.chat.completions.create(**request)
            async for chunk in response:
                if chunk.usage is not None:
                    usage = TokenUsage(chunk.usage.prompt_tokens, chunk.usage.completion_tokens)
                for choice in chunk.choices:
                    delta = choice.delta
                    if delta.content:
                        yield TextDelta(delta.content)
                    for fragment in delta.tool_calls or []:
                        entry = pending.setdefault(fragment.index, {"id": "", "name": "", "args": ""})
                        entry["id"] = fragment.id or entry["id"]
                        if fragment.function:
                            entry["name"] += fragment.function.name or ""
                            entry["args"] += fragment.function.arguments or ""
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
        except openai.APITimeoutError as exc:  # before APIConnectionError, its parent
            raise LLMTimeoutError("LLM request timed out") from exc
        except openai.APIStatusError as exc:
            raise LLMUnavailableError(f"LLM provider answered HTTP {exc.status_code}") from exc
        except openai.OpenAIError as exc:
            raise LLMUnavailableError(f"LLM provider call failed: {type(exc).__name__}") from exc

        if finish_reason is None:
            raise LLMUnavailableError("LLM stream ended without a finish reason")
        for index in sorted(pending):
            yield ToolCallRequested(self._assemble_tool_call(pending[index]))
        yield Completed(_FINISH_REASONS.get(finish_reason, "other"), usage)

    @staticmethod
    def _assemble_tool_call(entry: dict[str, str]) -> ToolCall:
        try:
            arguments = json.loads(entry["args"] or "{}")
        except json.JSONDecodeError as exc:
            raise LLMUnavailableError(f"Malformed arguments for tool {entry['name']!r}") from exc
        if not isinstance(arguments, dict):
            raise LLMUnavailableError(f"Arguments for tool {entry['name']!r} are not an object")
        return ToolCall(id=entry["id"], name=entry["name"], arguments=arguments)

    def _to_openai_message(self, message: LLMMessage) -> dict[str, Any]:
        if message.role is LLMRole.USER:
            return {"role": "user", "content": [self._to_openai_part(p) for p in message.content]}
        text = "".join(p.text for p in message.content if isinstance(p, TextPart))
        if message.role is LLMRole.TOOL:
            return {"role": "tool", "tool_call_id": message.tool_call_id, "content": text}
        result: dict[str, Any] = {"role": message.role.value, "content": text or None}
        if message.tool_calls:
            result["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in message.tool_calls
            ]
        return result

    def _to_openai_part(self, part: TextPart | ImagePart) -> dict[str, Any]:
        if isinstance(part, TextPart):
            return {"type": "text", "text": part.text}
        if not self._vision_enabled:
            raise LLMUnavailableError("Image content sent while vision is disabled")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{part.media_type};base64,{part.data_base64}"},
        }

    @staticmethod
    def _to_openai_tool(tool: ToolDefinition) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
