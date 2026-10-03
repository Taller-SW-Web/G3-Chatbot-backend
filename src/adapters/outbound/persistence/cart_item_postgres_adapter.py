"""Adapter cart_item (H2-03, SPEC-10, SPEC-11).

Operaciones minimas derivadas de modelo-datos.md (seccion `cart_item`).
No hace commit: la transaccion la controla el caso de uso. La validacion
de stock (SPEC-10) y los topes (10 u/linea, 20 lineas) los aplica el caso
de uso antes de llamar aqui.
"""
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.cart_item import CartItem


class CartItemPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def guardar(self, row: CartItem) -> CartItem:
        self.session.add(row)
        await self.session.flush()
        return row

    async def obtener_por_id(self, id: uuid.UUID) -> CartItem | None:
        stmt = select(CartItem).where(CartItem.id == id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def obtener_por_cart_y_sku(self, cart_id: uuid.UUID, sku: str) -> CartItem | None:
        # Agregar el mismo SKU suma cantidad en lugar de duplicar la linea.
        stmt = select(CartItem).where(CartItem.cart_id == cart_id, CartItem.sku == sku).limit(1)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def listar_por_cart(self, cart_id: uuid.UUID) -> list[CartItem]:
        stmt = select(CartItem).where(CartItem.cart_id == cart_id).order_by(CartItem.created_at)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def eliminar(self, id: uuid.UUID) -> bool:
        stmt = delete(CartItem).where(CartItem.id == id)
        res = await self.session.execute(stmt)
        await self.session.flush()
        return res.rowcount > 0
