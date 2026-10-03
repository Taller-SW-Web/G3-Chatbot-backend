"""Adapter claim_ref (H2-04 David, SPEC-19, SPEC-20).

Referencia local idempotente al reclamo de Ventas. No hace commit: la
transaccion la controla el caso de uso.
"""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.claim_ref import ClaimRef


class ClaimRefPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def guardar(self, row: ClaimRef) -> ClaimRef:
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError:
            # UNIQUE (idempotency_key): reintento con la misma clave.
            await self.session.rollback()
            existing = await self.obtener_por_idempotency_key(row.idempotency_key)
            if existing is None:
                raise
            return existing
        return row

    async def obtener_por_claim_id(self, claim_id: str) -> ClaimRef | None:
        stmt = select(ClaimRef).where(ClaimRef.claim_id == claim_id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def obtener_por_code(self, code: str) -> ClaimRef | None:
        stmt = select(ClaimRef).where(ClaimRef.code == code)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def obtener_por_idempotency_key(self, idempotency_key: str) -> ClaimRef | None:
        stmt = select(ClaimRef).where(ClaimRef.idempotency_key == idempotency_key)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()
