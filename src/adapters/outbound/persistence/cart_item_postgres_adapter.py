"""Adapter cart_item (SPEC-10, SPEC-11, cart_repository_port).

Implements `CartItemRepository`: it builds the rows and translates SQLAlchemy
errors into the port errors, so the use case never imports the ORM. It does
not commit (the use case owns the transaction). Stock validation (SPEC-10)
and the caps (10 u/line, 20 lines) are enforced by the use case before
calling here.
"""
import uuid

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from src.adapters.outbound.persistence.models.cart_item import CartItem
from src.ports.outbound.cart_repository_port import (
    CartConflictError,
    StaleCartError,
)


class CartItemPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _flush(self) -> None:
        try:
            await self.session.flush()
        except StaleDataError as exc:
            raise StaleCartError(str(exc)) from exc
        except IntegrityError as exc:
            # uq_cart_item_cart_sku: the same SKU raced into two rows.
            raise CartConflictError(str(exc)) from exc

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

    async def agregar(
        self,
        *,
        cart_id: uuid.UUID,
        product_id: str,
        sku: str,
        name: str,
        variant_description: str | None,
        image_url: str | None,
        quantity: int,
        unit_price_ref: float,
    ) -> CartItem:
        row = CartItem(
            cart_id=cart_id,
            product_id=product_id,
            sku=sku,
            name=name,
            variant_description=variant_description,
            image_url=image_url,
            quantity=quantity,
            unit_price_ref=unit_price_ref,
        )
        self.session.add(row)
        await self._flush()
        return row

    async def set_quantity(
        self, item_id: uuid.UUID, *, quantity: int, unit_price_ref: float
    ) -> None:
        row = await self.obtener_por_id(item_id)
        if row is None:
            return
        row.quantity = quantity
        row.unit_price_ref = unit_price_ref
        await self._flush()

    async def eliminar(self, id: uuid.UUID) -> bool:
        stmt = delete(CartItem).where(CartItem.id == id)
        res = await self.session.execute(stmt)
        await self._flush()
        return res.rowcount > 0
