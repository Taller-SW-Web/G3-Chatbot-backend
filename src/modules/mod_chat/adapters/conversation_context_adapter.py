"""Implementation of `ConversationContextPort` owned by `mod-chat`.

The adapters underneath are the shared persistence ports for conversations and
messages; only the operations cart/catalog need are exposed through this port.
"""
import uuid
from typing import Any

from src.shared.application.ports.conversation_context_port import ConversationContextPort
from src.shared.application.ports.conversation_repository_port import ConversationRepositoryPort
from src.shared.application.ports.message_repository_port import MessageRepositoryPort
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.domain.entities.message import MessageRole


class ConversationContextAdapter(ConversationContextPort):
    def __init__(
        self,
        conversations: ConversationRepositoryPort,
        messages: MessageRepositoryPort,
    ):
        self._conversations = conversations
        self._messages = messages

    async def find_owned(
        self, conversation_id: uuid.UUID, owner: CallerIdentity
    ) -> Any | None:
        return await self._conversations.find_owned(conversation_id, owner)

    async def get_context(self, conversation_id: uuid.UUID) -> dict[str, Any]:
        return await self._conversations.get_context(conversation_id)

    async def get_context_for_update(self, conversation_id: uuid.UUID) -> dict[str, Any]:
        return await self._conversations.get_context_for_update(conversation_id)

    async def save_context(
        self, conversation_id: uuid.UUID, context: dict[str, Any]
    ) -> None:
        await self._conversations.save_context(conversation_id, context)

    async def touch(self, conversation_id: uuid.UUID) -> None:
        await self._conversations.touch(conversation_id)

    async def add_tool_audit(
        self, conversation_id: uuid.UUID, *, tool: str, code: str, text: str
    ) -> None:
        await self._messages.add(
            conversation_id,
            MessageRole.TOOL,
            text,
            tool_name=tool,
            result_code=code,
        )
