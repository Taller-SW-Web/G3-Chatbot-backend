"""Outbound port: message storage (SPEC-05 Req. 1)."""
import uuid
from typing import Any, Protocol

from src.domain.entities.message import Message, MessageRole


class MessageRepositoryPort(Protocol):
    async def list_recent(self, conversation_id: uuid.UUID, *, limit: int) -> list[Message]:
        """Latest `limit` messages in chronological order."""
        ...

    async def list_dialogue(self, conversation_id: uuid.UUID, *, limit: int) -> list[Message]:
        """Latest `limit` CUSTOMER and ASSISTANT messages (tool rows excluded), chronological."""
        ...

    async def add(
        self,
        conversation_id: uuid.UUID,
        role: MessageRole,
        text: str,
        *,
        blocks: dict[str, Any] | None = None,
        tool_name: str | None = None,
        arguments: dict[str, Any] | None = None,
        result_code: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: int | None = None,
    ) -> Message:
        """Insert one message. Implementations do not commit."""
        ...
