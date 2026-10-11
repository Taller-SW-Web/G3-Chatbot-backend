"""Outbound port: conversation storage (SPEC-05 Req. 1). Implementations do not commit."""
import uuid
from typing import Protocol

from src.domain.entities.conversation import Conversation, ConversationSummary
from src.domain.value_objects.caller_identity import CallerIdentity


class ConversationRepositoryPort(Protocol):
    async def create(self, owner: CallerIdentity) -> Conversation:
        """Insert an empty conversation (no title) owned by `owner`."""
        ...

    async def list_summaries(
        self, owner: CallerIdentity, *, limit: int, offset: int
    ) -> list[ConversationSummary]:
        """Titled conversations of `owner`, most recent activity first."""
        ...

    async def find_owned(
        self, conversation_id: uuid.UUID, owner: CallerIdentity
    ) -> Conversation | None:
        """None when the conversation does not exist or belongs to someone else."""
        ...

    async def assign_title_if_missing(self, conversation_id: uuid.UUID, title: str) -> bool:
        """Set the title only while it is still empty. True when this call set it."""
        ...

    async def touch(self, conversation_id: uuid.UUID) -> None:
        """Move `last_message_at` to now (sidebar ordering)."""
        ...
