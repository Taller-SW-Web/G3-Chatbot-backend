"""Outbound port: LLM with tool calling and streaming (ADR-0005). Provider-neutral: no SDK imports here."""
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Protocol


class LLMError(Exception):
    """Base of the provider-neutral errors; the agent loop catches this to degrade (DegradedMode)."""


class LLMTimeoutError(LLMError):
    """The provider did not answer within the configured timeout."""


class LLMUnavailableError(LLMError):
    """The provider failed: missing or rejected credentials, rate limit, 5xx, malformed tool arguments."""


class LLMRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    """Inline image (ADR-0005 amendment): base64 only, never a URL."""

    media_type: str  # e.g. "image/jpeg"
    data_base64: str


ContentPart = TextPart | ImagePart


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMMessage:
    role: LLMRole
    content: tuple[ContentPart, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()  # only on ASSISTANT messages
    tool_call_id: str | None = None  # only on TOOL messages: the call being answered


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema of the arguments object


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallRequested:
    """Emitted once per call, after its streamed fragments were assembled and parsed."""

    call: ToolCall


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int


FinishReason = Literal["stop", "tool_calls", "length", "content_filter", "other"]


@dataclass(frozen=True)
class Completed:
    """Always the last event of a stream."""

    finish_reason: FinishReason
    usage: TokenUsage | None = field(default=None)


LLMEvent = TextDelta | ToolCallRequested | Completed


class LLMProvider(Protocol):
    @property
    def supports_vision(self) -> bool:
        """Whether the configured model accepts ImagePart content."""
        ...

    def stream(
        self, messages: Sequence[LLMMessage], tools: Sequence[ToolDefinition]
    ) -> AsyncIterator[LLMEvent]:
        """Stream one model turn. Raises LLMTimeoutError or LLMUnavailableError, also mid-stream."""
        ...
