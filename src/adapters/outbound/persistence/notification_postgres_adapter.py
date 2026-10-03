"""Adapter notification (H2-04 David, SPEC-16).

Registro de confirmaciones y reenvios (maximo 2 por pedido). No hace
commit: la transaccion la controla el caso de uso. El envio real lo
ejecuta el worker sobre el outbox (tarea SEND_EMAIL de Sonny).
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.notification import Notification


class NotificationPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def guardar(self, row: Notification) -> Notification:
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError:
            # UNIQUE (order_id, resend_number): el envio ya esta registrado.
            await self.session.rollback()
            existing = await self.obtener_por_order_y_reenvio(row.order_id, row.resend_number)
            if existing is None:
                raise
            return existing
        return row

    async def obtener_por_order_y_reenvio(
        self, order_id: str, resend_number: int
    ) -> Notification | None:
        stmt = select(Notification).where(
            Notification.order_id == order_id,
            Notification.resend_number == resend_number,
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def listar_por_order(self, order_id: str) -> list[Notification]:
        stmt = (
            select(Notification)
            .where(Notification.order_id == order_id)
            .order_by(Notification.resend_number)
        )
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def marcar_enviada(self, order_id: str, resend_number: int) -> Notification | None:
        row = await self.obtener_por_order_y_reenvio(order_id, resend_number)
        if row is None:
            return None
        row.status = "SENT"
        row.sent_at = datetime.now(timezone.utc)
        row.updated_at = datetime.now(timezone.utc)
        await self.session.flush()
        return row

    async def marcar_fallida(
        self, order_id: str, resend_number: int, last_error: str
    ) -> Notification | None:
        row = await self.obtener_por_order_y_reenvio(order_id, resend_number)
        if row is None:
            return None
        row.status = "FAILED"
        row.attempts = (row.attempts or 0) + 1
        row.last_error = last_error
        row.updated_at = datetime.now(timezone.utc)
        await self.session.flush()
        return row
