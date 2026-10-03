"""Adapter return_ref (H2-04 David, SPEC-21, SPEC-22).

Referencia local idempotente a la devolucion de Ventas. Traduce el tipo
en ambos sentidos (EN <-> ES) sin inventar literales. No hace commit.
"""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.return_ref import ReturnRef


class ReturnRefPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def tipo_desde_ventas(tipo: str) -> str:
        """Normaliza el literal de Ventas al valor local en ingles.

        Ventas usa CAMBIO/DEVOLUCION_DINERO; la base guarda
        EXCHANGE/MONEY_REFUND. No inventa traducciones no confirmadas.
        """
        if tipo == "CAMBIO":
            return "EXCHANGE"
        if tipo == "DEVOLUCION_DINERO":
            return "MONEY_REFUND"
        return tipo

    @staticmethod
    def tipo_hacia_ventas(tipo: str) -> str:
        """Usar al ensamblar la solicitud hacia Ventas."""
        if tipo == "EXCHANGE":
            return "CAMBIO"
        if tipo == "MONEY_REFUND":
            return "DEVOLUCION_DINERO"
        return tipo

    async def guardar(self, row: ReturnRef) -> ReturnRef:
        row.requested_type = self.tipo_desde_ventas(row.requested_type)
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

    async def obtener_por_return_id(self, return_id: str) -> ReturnRef | None:
        stmt = select(ReturnRef).where(ReturnRef.return_id == return_id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def obtener_por_code(self, code: str) -> ReturnRef | None:
        stmt = select(ReturnRef).where(ReturnRef.code == code)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def obtener_por_idempotency_key(self, idempotency_key: str) -> ReturnRef | None:
        stmt = select(ReturnRef).where(ReturnRef.idempotency_key == idempotency_key)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()
