"""Adapter notification (SPEC-16).

Registro de confirmaciones y reenvios (maximo 2 por pedido). No hace
commit: la transaccion la controla el caso de uso. El envio real lo
ejecuta el worker sobre el outbox (tarea SEND_EMAIL de Sonny).
"""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.notification import Notification
from src.ports.outbound.notification_repository_port import NotificationRecord

CONFIRMATION = "ORDER_CONFIRMATION"
RESEND = "CONFIRMATION_RESEND"


def _record(row: Notification) -> NotificationRecord:
    return NotificationRecord(
        order_id=row.order_id,
        resend_number=row.resend_number,
        recipient=row.recipient,
        status=row.status,
        attempts=row.attempts or 0,
        last_error=row.last_error,
    )


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

    # -- NotificationRepositoryPort (NotificationService, SPEC-16) -------------

    async def crear_si_no_existe(
        self, order_id: str, resend_number: int, recipient: str
    ) -> NotificationRecord:
        # ON CONFLICT DO NOTHING keeps the transaction alive (guardar() rolls back).
        stmt = (
            insert(Notification)
            .values(
                order_id=order_id,
                type=CONFIRMATION if resend_number == 0 else RESEND,
                resend_number=resend_number,
                recipient=recipient,
            )
            .on_conflict_do_nothing(constraint="uq_notification_order_resend")
        )
        await self.session.execute(stmt)
        row = await self.obtener_por_order_y_reenvio(order_id, resend_number)
        assert row is not None  # INSERT ... ON CONFLICT garantiza la fila
        return _record(row)

    async def obtener(self, order_id: str, resend_number: int) -> NotificationRecord | None:
        row = await self.obtener_por_order_y_reenvio(order_id, resend_number)
        return _record(row) if row is not None else None

    async def contar_reenvios(self, order_id: str) -> int:
        stmt = select(func.count()).select_from(Notification).where(
            Notification.order_id == order_id, Notification.type == RESEND
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def registrar_reintento(
        self, order_id: str, resend_number: int, last_error: str
    ) -> Notification | None:
        row = await self.obtener_por_order_y_reenvio(order_id, resend_number)
        if row is None:
            return None
        row.attempts = (row.attempts or 0) + 1
        row.last_error = last_error
        row.updated_at = datetime.now(timezone.utc)
        await self.session.flush()
        return row

    async def cerrar_fallida(
        self, order_id: str, resend_number: int, last_error: str
    ) -> Notification | None:
        row = await self.obtener_por_order_y_reenvio(order_id, resend_number)
        if row is None:
            return None
        row.status = "FAILED"
        row.last_error = last_error
        row.updated_at = datetime.now(timezone.utc)
        await self.session.flush()
        return row
