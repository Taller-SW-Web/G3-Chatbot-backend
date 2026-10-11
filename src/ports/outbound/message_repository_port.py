"""Outbound port: message storage (SPEC-05 Req. 1)."""
import uuid
from typing import Protocol

from src.domain.entities.message import Message


class MessageRepositoryPort(Protocol):
    async def list_recent(self, conversation_id: uuid.UUID, *, limit: int) -> list[Message]:
        """Latest `limit` messages in chronological order."""
        ...
