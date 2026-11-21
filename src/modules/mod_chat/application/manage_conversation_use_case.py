"""Conversation operations of SPEC-05 Req. 1: create, list, open history (ADR-0018)."""
import uuid

from src.modules.mod_chat.ports.chatbot_service_port import ConversationNotFoundError
from src.shared.application.ports.conversation_repository_port import ConversationRepositoryPort
from src.shared.application.ports.message_repository_port import MessageRepositoryPort
from src.shared.application.unit_of_work_port import UnitOfWorkPort
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.domain.entities.conversation import Conversation, ConversationSummary
from src.shared.domain.entities.message import Message, MessageRole

# Provisional values: the specs leave them undefined.
CONVERSATIONS_PAGE_SIZE = 20
HISTORY_LIMIT = 50  # "sus ultimos 50 mensajes" (SPEC-05 Req. 1, Retomar una conversacion)


class ManageConversationUseCase:
    def __init__(
        self,
        conversations: ConversationRepositoryPort,
        messages: MessageRepositoryPort,
        uow: UnitOfWorkPort,
        attachments=None,
    ):
        self._conversations = conversations
        self._messages = messages
        self._uow = uow
        self._attachments = attachments

    async def create_conversation(self, caller: CallerIdentity) -> Conversation:
        try:
            conversation = await self._conversations.create(caller)
            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise
        return conversation

    async def list_conversations(
        self, caller: CallerIdentity | None, page: int
    ) -> list[ConversationSummary]:
        if caller is None:  # visitor without chat_sid owns nothing
            return []
        return await self._conversations.list_summaries(
            caller, limit=CONVERSATIONS_PAGE_SIZE, offset=(max(page, 1) - 1) * CONVERSATIONS_PAGE_SIZE
        )

    async def search_conversations(
        self, caller: CallerIdentity | None, query: str, page: int
    ) -> list[ConversationSummary]:
        if caller is None:  # visitor without chat_sid owns nothing
            return []
        return await self._conversations.search_summaries(
            caller, query, limit=CONVERSATIONS_PAGE_SIZE, offset=(max(page, 1) - 1) * CONVERSATIONS_PAGE_SIZE
        )

    async def get_history(
        self, caller: CallerIdentity | None, conversation_id: uuid.UUID
    ) -> list[Message]:
        if caller is None:
            raise ConversationNotFoundError(conversation_id)
        if await self._conversations.find_owned(conversation_id, caller) is None:
            raise ConversationNotFoundError(conversation_id)
        messages = await self._messages.list_recent(conversation_id, limit=HISTORY_LIMIT)
        # Tool-call rows are internal; the client only renders customer and assistant turns.
        visible = [m for m in messages if m.role is not MessageRole.TOOL]
        if self._attachments is not None:
            return await self._attachments.attach_history(visible)
        return visible
