"""Adapter mínimo celular_verificacion_local (H2-02).

Alcance H2-02 mínimo acordado: modelo + adapter con 2 operaciones + 1 test.
No incluye OTP/SMS/CheckoutGuard (eso es Hito 4 SPEC-04 completo).

Guard futuro (SPEC-04 Req.1): SELECT 1 WHERE cliente_id=:sub AND celular=:vigente.
Si 0 filas -> 403 CELULAR_NO_VERIFICADO.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.celular_verificacion import CelularVerificacionLocal


class CelularVerificacionPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def existe(self, cliente_id: uuid.UUID, celular: str) -> bool:
        stmt = select(CelularVerificacionLocal.id).where(
            CelularVerificacionLocal.cliente_id == cliente_id,
            CelularVerificacionLocal.celular == celular,
        ).limit(1)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none() is not None

    async def guardar(self, cliente_id: uuid.UUID, celular: str) -> CelularVerificacionLocal:
        row = CelularVerificacionLocal(
            cliente_id=cliente_id,
            celular=celular,
            verificado_en=datetime.now(timezone.utc),
        )
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError:
            # UNIQUE (cliente_id, celular): ya verificado, devuelve el existente sin duplicar.
            await self.session.rollback()
            stmt = select(CelularVerificacionLocal).where(
                CelularVerificacionLocal.cliente_id == cliente_id,
                CelularVerificacionLocal.celular == celular,
            ).limit(1)
            res = await self.session.execute(stmt)
            existing = res.scalar_one()
            return existing
        return row
