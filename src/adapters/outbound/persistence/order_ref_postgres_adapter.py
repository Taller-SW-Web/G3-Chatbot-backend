"""Adapter order_ref (H2-03, SPEC-15).

Operaciones minimas derivadas de modelo-datos.md (seccion `order_ref`).
No hace commit: la transaccion la controla el caso de uso. La creacion del
pedido en Ventas y la notificacion de pago comparten transaccion con el
outbox (patron transaccional, ADR-0011).
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.order_ref import OrderRef


class OrderRefPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def estado_desde_ventas(estado: str) -> str:
        """Normaliza el literal de Ventas al valor local en ingles.

        Ventas usa ANULADO; la base guarda CANCELLED. No inventa
        traducciones para estados aun no confirmados.
        """
        return "CANCELLED" if estado == "ANULADO" else estado

    @staticmethod
    def estado_hacia_ventas(estado: str) -> str:
        """Usar al interpretar estados que viajan hacia Ventas."""
        return "ANULADO" if estado == "CANCELLED" else estado

    async def guardar(self, row: OrderRef) -> OrderRef:
        row.local_status = self.estado_desde_ventas(row.local_status)
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError:
            # UNIQUE (checkout_id): el checkout ya genero su unico pedido.
            await self.session.rollback()
            existing = await self.obtener_por_checkout_id(row.checkout_id)
            if existing is None:
                raise
            return existing
        return row

    async def obtener_por_order_id(self, order_id: str) -> OrderRef | None:
        stmt = select(OrderRef).where(OrderRef.order_id == order_id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def obtener_por_checkout_id(self, checkout_id: uuid.UUID) -> OrderRef | None:
        # Un checkout genera como maximo un pedido.
        stmt = select(OrderRef).where(OrderRef.checkout_id == checkout_id).limit(1)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def actualizar_estado(self, order_id: str, local_status: str) -> OrderRef | None:
        row = await self.obtener_por_order_id(order_id)
        if row is None:
            return None
        row.local_status = self.estado_desde_ventas(local_status)
        row.updated_at = datetime.now(timezone.utc)
        await self.session.flush()
        return row
