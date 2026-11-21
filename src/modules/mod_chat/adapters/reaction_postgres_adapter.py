"""Adapter de reacciones de mensajes (SPEC-24)."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.infrastructure.db.models.feedback import MessageReaction


class ReactionPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(
        self,
        message_id: uuid.UUID,
        *,
        customer_id: uuid.UUID | None,
        anonymous_sid: str | None,
        reaction: str,
    ) -> MessageReaction:
        stmt = select(MessageReaction).where(
            MessageReaction.message_id == message_id,
            (
                MessageReaction.customer_id == customer_id
                if customer_id is not None
                else MessageReaction.customer_id.is_(None)
            ),
            (
                MessageReaction.anonymous_sid == anonymous_sid
                if anonymous_sid is not None
                else MessageReaction.anonymous_sid.is_(None)
            ),
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = MessageReaction(
                message_id=message_id,
                customer_id=customer_id,
                anonymous_sid=anonymous_sid,
                reaction=reaction,
            )
            self.session.add(row)
        else:
            row.reaction = reaction
            row.updated_at = datetime.now(timezone.utc)
        await self.session.flush()
        return row

    async def obtener(self, message_id: uuid.UUID, *, customer_id=None, anonymous_sid=None):
        stmt = select(MessageReaction).where(
            MessageReaction.message_id == message_id,
            (
                MessageReaction.customer_id == customer_id
                if customer_id is not None
                else MessageReaction.customer_id.is_(None)
            ),
            (
                MessageReaction.anonymous_sid == anonymous_sid
                if anonymous_sid is not None
                else MessageReaction.anonymous_sid.is_(None)
            ),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()
