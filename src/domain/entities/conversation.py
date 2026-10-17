"""Conversation entities (SPEC-05 Req. 1). Pure domain: no framework imports."""
import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Conversation:
    id: uuid.UUID
    title: str | None
    last_message_at: datetime
    summary: str | None = None  # rolling summary of older messages, fed to the LLM (ADR-0005)


@dataclass(frozen=True)
class HighlightSegment:
    """Piece of a search result; `match` marks the words that matched the query."""

    text: str
    match: bool


@dataclass(frozen=True)
class SearchHighlight:
    """Search result highlighting (SPEC-05 Req. 1 · Buscar chats).

    `snippet` is a fragment of the most recent matching customer/assistant message,
    or None when only the title matched."""

    title: tuple[HighlightSegment, ...]
    snippet: tuple[HighlightSegment, ...] | None


@dataclass(frozen=True)
class ConversationSummary:
    """Sidebar row: a conversation that already has a title, plus its last-message preview."""

    id: uuid.UUID
    title: str
    preview: str
    last_message_at: datetime
    highlight: SearchHighlight | None = None  # only set by search
