"""Persistencia async de mensajes; transacción controlada por el llamador.

El caso de uso verifica pertenencia y filtra el texto antes de llamar guardar.
Este adapter se inyecta junto al adapter de conversaciones de Mathias.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.message import Message


class MessagePostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def guardar(self, message: Message) -> Message:
        """Recibe texto redactado y argumentos validados; no cierra la transacción."""
        self.session.add(message)
        await self.session.flush()
        return message

    async def listar_recientes(
        self, conversation_id: uuid.UUID, *, limite: int = 50
    ) -> list[Message]:
        """Últimos N mensajes, devueltos en orden cronológico (SPEC-05 Req. 1)."""
        if not 1 <= limite <= 50:
            raise ValueError("limite debe estar entre 1 y 50")
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limite)
        )
        result = await self.session.execute(stmt)
        return list(reversed(result.scalars().all()))
