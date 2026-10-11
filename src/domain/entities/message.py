"""Message entity (SPEC-05). Pure domain: no framework imports."""
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class MessageRole(str, Enum):
    CUSTOMER = "CUSTOMER"
    ASSISTANT = "ASSISTANT"
    TOOL = "TOOL"


@dataclass(frozen=True)
class Message:
    id: uuid.UUID
    conversation_id: uuid.UUID
    role: MessageRole
    text: str
    blocks: Any | None
    created_at: datetime
