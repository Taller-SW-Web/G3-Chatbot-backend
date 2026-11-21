"""Adapter attachment (SPEC-23).

Operaciones minimas derivadas de modelo-datos.md (seccion `attachment`). No hace commit: la
transaccion la controla el caso de uso. La unica modificacion permitida de una
fila es vincular_a_mensaje (PENDING -> SENT).
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.infrastructure.db.models.attachment import Attachment


class AttachmentPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def guardar(self, row: Attachment) -> Attachment:
        self.session.add(row)
        await self.session.flush()
        return row

    async def obtener_por_id(self, id: uuid.UUID) -> Attachment | None:
        stmt = select(Attachment).where(Attachment.id == id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def listar_por_mensaje(self, message_id: uuid.UUID) -> list[Attachment]:
        stmt = select(Attachment).where(Attachment.message_id == message_id).order_by(Attachment.created_at)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def vincular_a_mensaje(self, attachment_id: uuid.UUID, message_id: uuid.UUID) -> Attachment | None:
        row = await self.obtener_por_id(attachment_id)
        if row is None or row.status != "PENDING":
            return None
        row.message_id = message_id
        row.status = "SENT"
        row.updated_at = datetime.now(timezone.utc)
        await self.session.flush()
        return row

    async def listar_pendientes_antes_de(self, fecha: datetime) -> list[Attachment]:
        # Job de limpieza: adjuntos subidos y nunca enviados.
        stmt = (
            select(Attachment)
            .where(Attachment.status == "PENDING", Attachment.created_at < fecha)
            .order_by(Attachment.created_at)
        )
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def contar_pendientes(self, conversation_id: uuid.UUID) -> int:
        stmt = select(func.count(Attachment.id)).where(
            Attachment.conversation_id == conversation_id, Attachment.status == "PENDING"
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def listar_por_mensajes(self, message_ids: list[uuid.UUID]) -> list[Attachment]:
        if not message_ids:
            return []
        stmt = (
            select(Attachment)
            .where(Attachment.message_id.in_(message_ids))
            .order_by(Attachment.created_at)
        )
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def eliminar(self, attachment_id: uuid.UUID) -> None:
        row = await self.obtener_por_id(attachment_id)
        if row is not None:
            await self.session.delete(row)
            await self.session.flush()

    async def listar_claves(self) -> set[str]:
        stmt = select(Attachment.storage_key, Attachment.thumbnail_key)
        res = await self.session.execute(stmt)
        return {key for row in res.all() for key in row if key}
