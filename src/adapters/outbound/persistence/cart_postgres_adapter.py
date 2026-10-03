"""Adapter cart (H2-03, SPEC-11).

Operaciones minimas derivadas de modelo-datos.md (seccion `cart`). No hace
commit: la transaccion la controla el caso de uso. El bloqueo optimista
(version) lo aplica el caso de uso al actualizar.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.cart import Cart


class CartPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def guardar(self, row: Cart) -> Cart:
        self.session.add(row)
        await self.session.flush()
        return row

    async def obtener_por_id(self, id: uuid.UUID) -> Cart | None:
        stmt = select(Cart).where(Cart.id == id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def obtener_abierto_por_cliente(self, customer_id: uuid.UUID) -> Cart | None:
        # Un solo carrito abierto por cliente (ACTIVE o IN_CHECKOUT).
        stmt = select(Cart).where(
            Cart.customer_id == customer_id,
            Cart.status.in_(["ACTIVE", "IN_CHECKOUT"]),
        ).limit(1)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def obtener_abierto_por_conversacion(self, conversation_id: uuid.UUID) -> Cart | None:
        # Un solo carrito anonimo abierto por conversacion (fusion al login).
        stmt = select(Cart).where(
            Cart.conversation_id == conversation_id,
            Cart.customer_id.is_(None),
            Cart.status.in_(["ACTIVE", "IN_CHECKOUT"]),
        ).limit(1)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def cambiar_estado(self, cart_id: uuid.UUID, status: str) -> Cart | None:
        row = await self.obtener_por_id(cart_id)
        if row is None:
            return None
        row.status = status
        row.updated_at = datetime.now(timezone.utc)
        await self.session.flush()
        return row
