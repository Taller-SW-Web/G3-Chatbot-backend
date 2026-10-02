"""Adapter attachment (H2-07, SPEC-23).

Operaciones minimas derivadas de modelo-datos.md (seccion `attachment`). No hace commit: la
transaccion la controla el caso de uso. La unica modificacion permitida de una
fila es vincular_a_mensaje (PENDING -> SENT).
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.attachment import Attachment


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
