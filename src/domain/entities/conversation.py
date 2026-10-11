"""Conversation entities (SPEC-05 Req. 1). Pure domain: no framework imports."""
import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Conversation:
    id: uuid.UUID
    title: str | None
    last_message_at: datetime


@dataclass(frozen=True)
class ConversationSummary:
    """Sidebar row: a conversation that already has a title, plus its last-message preview."""

    id: uuid.UUID
    title: str
    preview: str
    last_message_at: datetime
