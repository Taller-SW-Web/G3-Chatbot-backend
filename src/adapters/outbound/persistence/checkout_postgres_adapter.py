"""Adapter checkout (H2-04 David, SPEC-12, SPEC-14).

Operaciones minimas derivadas de modelo-datos.md (seccion `checkout`).
No hace commit: la transaccion la controla el caso de uso. La creacion
del checkout, el intento de pago y la tarea outbox comparten transaccion
(patron transaccional, ADR-0011).
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.checkout import Checkout


class CheckoutPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def guardar(self, row: Checkout) -> Checkout:
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError:
            # UNIQUE parcial (cart_id vivo) o idempotency_key: el checkout
            # ya existe; se devuelve el vivo para no duplicar.
            await self.session.rollback()
            existing = await self.obtener_vivo_por_cart(row.cart_id)
            if existing is None:
                raise
            return existing
        return row

    async def obtener_por_id(self, id: uuid.UUID) -> Checkout | None:
        stmt = select(Checkout).where(Checkout.id == id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def obtener_vivo_por_cart(self, cart_id: uuid.UUID) -> Checkout | None:
        # Un solo checkout vivo por carrito (anti doble-clic).
        stmt = (
            select(Checkout)
            .where(
                Checkout.cart_id == cart_id,
                Checkout.status.in_(["PENDING_PAYMENT", "PAYMENT_APPROVED"]),
            )
            .order_by(Checkout.created_at.desc())
            .limit(1)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def marcar_estado(self, id: uuid.UUID, status: str) -> Checkout | None:
        row = await self.obtener_por_id(id)
        if row is None:
            return None
        row.status = status
        row.updated_at = datetime.now(timezone.utc)
        await self.session.flush()
        return row

    async def incrementar_intentos(self, id: uuid.UUID) -> Checkout | None:
        # El CHECK (payment_attempts BETWEEN 0 AND 3) topea en BD.
        row = await self.obtener_por_id(id)
        if row is None:
            return None
        row.payment_attempts = (row.payment_attempts or 0) + 1
        row.updated_at = datetime.now(timezone.utc)
        await self.session.flush()
        return row
