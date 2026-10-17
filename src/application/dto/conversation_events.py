"""Application-level events of one assistant turn (SPEC-05 Req. 4).

A WebSocket adapter maps them to the wire events: TokenEvent -> `token`, BlockEvent -> `bloque`,
FinalEvent -> `fin`. MessageAcceptedEvent and DegradedEvent are informational for the transport.
"""
from dataclasses import dataclass
from typing import Any, Literal

from src.domain.entities.message import Message

DegradedReason = Literal["timeout", "unavailable", "iteration_limit", "invalid_arguments"]


@dataclass(frozen=True)
class MessageAcceptedEvent:
    """The customer message is persisted; always the first event."""

    message: Message


@dataclass(frozen=True)
class TokenEvent:
    text: str


@dataclass(frozen=True)
class BlockEvent:
    block: dict[str, Any]


@dataclass(frozen=True)
class DegradedEvent:
    """The turn ended with a safe fallback reply instead of a normal answer.

    Transports must discard the tokens received earlier in the turn: the stored reply contains only the
    fallback text (plus the blocks of tools that already ran), never the partial streamed text.
    """

    reason: DegradedReason


@dataclass(frozen=True)
class FinalEvent:
    """Always the last event: the persisted assistant message, whose text is authoritative over the tokens."""

    message: Message


ConversationEvent = MessageAcceptedEvent | TokenEvent | BlockEvent | DegradedEvent | FinalEvent
