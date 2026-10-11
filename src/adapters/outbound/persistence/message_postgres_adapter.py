"""Persistencia async de mensajes; transacción controlada por el llamador.

El caso de uso verifica pertenencia y filtra el texto antes de llamar guardar.
Este adapter se inyecta junto al adapter de conversaciones de Mathias.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.message import Message
from src.domain.entities.message import Message as MessageEntity
from src.domain.entities.message import MessageRole


def _to_entity(r: Message) -> MessageEntity:
    return MessageEntity(
        id=r.id,
        conversation_id=r.conversation_id,
        role=MessageRole(r.role),
        text=r.content or "",
        blocks=r.blocks,
        created_at=r.created_at,
    )


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

    # Implements MessageRepositoryPort (English name, domain objects).
    async def list_recent(self, conversation_id: uuid.UUID, *, limit: int) -> list[MessageEntity]:
        rows = await self.listar_recientes(conversation_id, limite=limit)
        return [_to_entity(r) for r in rows]

    async def list_dialogue(self, conversation_id: uuid.UUID, *, limit: int) -> list[MessageEntity]:
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.role != MessageRole.TOOL.value)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return [_to_entity(r) for r in reversed(result.scalars().all())]

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
    ) -> MessageEntity:
        row = await self.guardar(
            Message(
                conversation_id=conversation_id,
                role=role.value,
                content=text,
                blocks=blocks,
                tool_name=tool_name,
                arguments=arguments,
                result_code=result_code,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                # Explicit clock: rows of one transaction must keep their insertion order (now() is frozen).
                created_at=datetime.now(timezone.utc),
            )
        )
        return _to_entity(row)
