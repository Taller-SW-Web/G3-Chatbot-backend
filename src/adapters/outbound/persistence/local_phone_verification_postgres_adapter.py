"""Adapter mínimo local_phone_verification (H2-02).

Alcance H2-02 mínimo acordado: modelo + adapter con 2 operaciones + 1 test.
No incluye OTP/SMS/CheckoutGuard (eso es Hito 4 SPEC-04 completo).

Guard futuro (SPEC-04 Req.1): SELECT 1 WHERE customer_id=:sub AND phone=:vigente.
Si 0 filas -> 403 CELULAR_NO_VERIFICADO.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.local_phone_verification import LocalPhoneVerification


class LocalPhoneVerificationPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def existe(self, customer_id: uuid.UUID, phone: str) -> bool:
        stmt = select(LocalPhoneVerification.id).where(
            LocalPhoneVerification.customer_id == customer_id,
            LocalPhoneVerification.phone == phone,
        ).limit(1)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none() is not None

    async def guardar(self, customer_id: uuid.UUID, phone: str) -> LocalPhoneVerification:
        row = LocalPhoneVerification(
            customer_id=customer_id,
            phone=phone,
            verified_at=datetime.now(timezone.utc),
        )
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError:
            # UNIQUE (customer_id, phone): ya verificado, devuelve el existente sin duplicar.
            await self.session.rollback()
            stmt = select(LocalPhoneVerification).where(
                LocalPhoneVerification.customer_id == customer_id,
                LocalPhoneVerification.phone == phone,
            ).limit(1)
            res = await self.session.execute(stmt)
            existing = res.scalar_one()
            return existing
        return row
