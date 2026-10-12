"""Inbound port: what HTTP adapters may call (ADR-0002). Conversation operations for now."""
import uuid
from typing import Protocol

from src.domain.entities.conversation import Conversation, ConversationSummary
from src.domain.entities.message import Message
from src.domain.value_objects.caller_identity import CallerIdentity


class ConversationNotFoundError(Exception):
    """Missing or not owned by the caller; the two cases are deliberately indistinguishable."""


class ChatbotServicePort(Protocol):
    async def create_conversation(self, caller: CallerIdentity) -> Conversation: ...

    async def list_conversations(
        self, caller: CallerIdentity | None, page: int
    ) -> list[ConversationSummary]: ...

    async def search_conversations(
        self, caller: CallerIdentity | None, query: str, page: int
    ) -> list[ConversationSummary]: ...

    async def get_history(
        self, caller: CallerIdentity | None, conversation_id: uuid.UUID
    ) -> list[Message]:
        """Raises ConversationNotFoundError."""
        ...
