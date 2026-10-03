"""Adapter outbox (H2-03, SPEC-15, SPEC-16).

Operaciones minimas derivadas de modelo-datos.md (seccion `outbox`).
No hace commit: la transaccion la controla el caso de uso (el INSERT del
outbox comparte transaccion con el intento de pago / checkout). El worker
de APScheduler usa listar_pendientes con SKIP LOCKED.
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.outbox import Outbox


class OutboxPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def guardar(self, row: Outbox) -> Outbox:
        self.session.add(row)
        await self.session.flush()
        return row

    async def obtener_por_id(self, id: uuid.UUID) -> Outbox | None:
        stmt = select(Outbox).where(Outbox.id == id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def listar_pendientes(self, limite: int = 100) -> list[Outbox]:
        # Polling del worker: PENDING vencidas, ordenadas por intento.
        # El FOR UPDATE SKIP LOCKED permite mas de un worker sin colision.
        stmt = (
            select(Outbox)
            .where(Outbox.status == "PENDING", Outbox.next_attempt_at <= func.now())
            .order_by(Outbox.next_attempt_at)
            .limit(limite)
            .with_for_update(skip_locked=True)
        )
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def marcar_procesado(self, id: uuid.UUID) -> Outbox | None:
        row = await self.obtener_por_id(id)
        if row is None:
            return None
        row.status = "PROCESSED"
        row.processed_at = datetime.now(timezone.utc)
        row.updated_at = datetime.now(timezone.utc)
        await self.session.flush()
        return row

    async def marcar_reintento(
        self, id: uuid.UUID, next_attempt_at: datetime, last_error: str | None = None
    ) -> Outbox | None:
        row = await self.obtener_por_id(id)
        if row is None:
            return None
        row.attempts += 1
        row.next_attempt_at = next_attempt_at
        row.last_attempt_at = datetime.now(timezone.utc)
        row.last_error = last_error
        if row.attempts >= 5:
            row.status = "FAILED"
        row.updated_at = datetime.now(timezone.utc)
        await self.session.flush()
        return row
