"""Adapter conversation (H2-07, SPEC-05).

Operaciones minimas derivadas de modelo-datos.md (seccion `conversation`). No hace commit: la
transaccion la controla el caso de uso.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.conversation import Conversation


class ConversationPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def guardar(self, row: Conversation) -> Conversation:
        self.session.add(row)
        await self.session.flush()
        return row

    async def obtener_por_id(self, id: uuid.UUID) -> Conversation | None:
        stmt = select(Conversation).where(Conversation.id == id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def listar_por_cliente(self, customer_id: uuid.UUID) -> list[Conversation]:
        # Solo conversaciones con title: las vacias no aparecen en la barra lateral.
        stmt = (
            select(Conversation)
            .where(Conversation.customer_id == customer_id, Conversation.title.is_not(None))
            .order_by(Conversation.last_message_at.desc())
        )
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def obtener_anonima_por_sid(self, anonymous_sid: str) -> Conversation | None:
        stmt = (
            select(Conversation)
            .where(Conversation.anonymous_sid == anonymous_sid, Conversation.customer_id.is_(None))
            .order_by(Conversation.last_message_at.desc())
            .limit(1)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()
