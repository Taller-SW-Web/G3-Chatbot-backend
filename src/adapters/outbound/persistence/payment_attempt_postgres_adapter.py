"""Adapter payment_attempt (H2-04 David, SPEC-14 Req. 5).

Solo insercion: el intento nunca se modifica. No hace commit: la
transaccion la controla el caso de uso (checkout + intento + outbox).
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.payment_attempt import PaymentAttempt


class PaymentAttemptPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def guardar(self, row: PaymentAttempt) -> PaymentAttempt:
        self.session.add(row)
        await self.session.flush()
        return row

    async def listar_por_checkout(self, checkout_id: uuid.UUID) -> list[PaymentAttempt]:
        stmt = (
            select(PaymentAttempt)
            .where(PaymentAttempt.checkout_id == checkout_id)
            .order_by(PaymentAttempt.created_at, PaymentAttempt.id)
        )
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def contar_por_checkout(self, checkout_id: uuid.UUID) -> int:
        return len(await self.listar_por_checkout(checkout_id))
