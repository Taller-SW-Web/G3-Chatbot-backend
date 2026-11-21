"""Inbound port: what HTTP adapters may call (ADR-0002). Conversation operations for now."""
import uuid
from typing import Protocol

from src.shared.domain.caller_identity import CallerIdentity
from src.shared.domain.entities.conversation import Conversation, ConversationSummary
from src.shared.domain.entities.message import Message


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
