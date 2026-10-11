"""Conversation operations of SPEC-05 Req. 1: create, list, open history (ADR-0018)."""
import uuid

from src.domain.entities.conversation import Conversation, ConversationSummary
from src.domain.entities.message import Message, MessageRole
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.inbound.chatbot_service_port import ConversationNotFoundError
from src.ports.outbound.conversation_repository_port import ConversationRepositoryPort
from src.ports.outbound.message_repository_port import MessageRepositoryPort
from src.ports.outbound.unit_of_work_port import UnitOfWorkPort

# Provisional values: the specs leave them undefined.
CONVERSATIONS_PAGE_SIZE = 20
HISTORY_LIMIT = 50  # "sus ultimos 50 mensajes" (SPEC-05 Req. 1, Retomar una conversacion)


class ManageConversationUseCase:
    def __init__(
        self,
        conversations: ConversationRepositoryPort,
        messages: MessageRepositoryPort,
        uow: UnitOfWorkPort,
    ):
        self._conversations = conversations
        self._messages = messages
        self._uow = uow

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

    async def get_history(
        self, caller: CallerIdentity | None, conversation_id: uuid.UUID
    ) -> list[Message]:
        if caller is None:
            raise ConversationNotFoundError(conversation_id)
        if await self._conversations.find_owned(conversation_id, caller) is None:
            raise ConversationNotFoundError(conversation_id)
        messages = await self._messages.list_recent(conversation_id, limit=HISTORY_LIMIT)
        # Tool-call rows are internal; the client only renders customer and assistant turns.
        return [m for m in messages if m.role is not MessageRole.TOOL]
