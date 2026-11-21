"""Adapter phone_otp_challenge (SPEC-04). No hace commit: lo controla el caso de uso."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.infrastructure.db.models.phone_otp_challenge import PhoneOtpChallenge


class PhoneOtpPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def crear(
        self,
        customer_id: uuid.UUID,
        phone: str,
        code_hash: str,
        expires_at: datetime,
    ) -> PhoneOtpChallenge:
        row = PhoneOtpChallenge(
            customer_id=customer_id,
            phone=phone,
            code_hash=code_hash,
            expires_at=expires_at,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def contar_desde(self, customer_id: uuid.UUID, since: datetime) -> int:
        stmt = select(func.count(PhoneOtpChallenge.id)).where(
            PhoneOtpChallenge.customer_id == customer_id,
            PhoneOtpChallenge.created_at >= since,
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def primer_envio_desde(self, customer_id: uuid.UUID, since: datetime) -> datetime | None:
        stmt = select(func.min(PhoneOtpChallenge.created_at)).where(
            PhoneOtpChallenge.customer_id == customer_id,
            PhoneOtpChallenge.created_at >= since,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def ultimo_vigente(
        self, customer_id: uuid.UUID, phone: str
    ) -> PhoneOtpChallenge | None:
        stmt = (
            select(PhoneOtpChallenge)
            .where(
                PhoneOtpChallenge.customer_id == customer_id,
                PhoneOtpChallenge.phone == phone,
                PhoneOtpChallenge.consumed_at.is_(None),
            )
            .order_by(PhoneOtpChallenge.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def invalidar_pendientes(self, customer_id: uuid.UUID, phone: str) -> None:
        stmt = (
            update(PhoneOtpChallenge)
            .where(
                PhoneOtpChallenge.customer_id == customer_id,
                PhoneOtpChallenge.phone == phone,
                PhoneOtpChallenge.consumed_at.is_(None),
            )
            .values(consumed_at=datetime.now(timezone.utc))
        )
        await self.session.execute(stmt)

    async def incrementar_intentos(self, challenge_id: uuid.UUID) -> int:
        row = await self.session.get(PhoneOtpChallenge, challenge_id)
        if row is None:
            return 0
        row.attempts += 1
        await self.session.flush()
        return row.attempts

    async def consumir(self, challenge_id: uuid.UUID) -> None:
        row = await self.session.get(PhoneOtpChallenge, challenge_id)
        if row is not None:
            row.consumed_at = datetime.now(timezone.utc)
            await self.session.flush()
