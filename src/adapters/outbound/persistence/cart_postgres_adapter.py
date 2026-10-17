"""Adapter cart (SPEC-11, cart_repository_port).

Implements `CartRepository`: it builds the rows and translates SQLAlchemy
errors into the port errors (`StaleCartError`, `CartConflictError`), so the
use case never imports the ORM. It does not commit: the transaction is the
use case's (`uow`). The optimistic `version` column is advanced by SQLAlchemy
`server_onupdate`, so `touch()` writes `updated_at` and lets the mapping bump
the version.
"""
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from src.adapters.outbound.persistence.models.cart import Cart
from src.ports.outbound.cart_repository_port import (
    CartConflictError,
    StaleCartError,
)

OPEN_STATUSES = ("ACTIVE", "IN_CHECKOUT")


class CartPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _flush(self) -> None:
        try:
            await self.session.flush()
        except StaleDataError as exc:
            raise StaleCartError(str(exc)) from exc
        except IntegrityError as exc:
            # One open cart per customer/conversation: a first concurrent
            # add can open two carts and hit the unique index.
            raise CartConflictError(str(exc)) from exc

    async def obtener_por_id(self, id: uuid.UUID) -> Cart | None:
        stmt = select(Cart).where(Cart.id == id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def obtener_abierto_por_cliente(self, customer_id: uuid.UUID) -> Cart | None:
        # Un solo carrito abierto por cliente (ACTIVE o IN_CHECKOUT).
        stmt = select(Cart).where(
            Cart.customer_id == customer_id,
            Cart.status.in_(OPEN_STATUSES),
        ).limit(1)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def obtener_abierto_por_conversacion(self, conversation_id: uuid.UUID) -> Cart | None:
        # Un solo carrito anonimo abierto por conversacion (fusion al login).
        stmt = select(Cart).where(
            Cart.conversation_id == conversation_id,
            Cart.customer_id.is_(None),
            Cart.status.in_(OPEN_STATUSES),
        ).limit(1)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def crear_para_cliente(self, customer_id: uuid.UUID) -> Cart:
        row = Cart(customer_id=customer_id)
        self.session.add(row)
        await self._flush()
        return row

    async def crear_anonimo(self, conversation_id: uuid.UUID) -> Cart:
        row = Cart(conversation_id=conversation_id)
        self.session.add(row)
        await self._flush()
        return row

    async def cambiar_estado(self, cart_id: uuid.UUID, status: str) -> Cart | None:
        row = await self.obtener_por_id(cart_id)
        if row is None:
            return None
        row.status = status
        row.updated_at = datetime.now(timezone.utc)
        await self._flush()
        return row

    async def set_totals(self, cart_id: uuid.UUID, snapshot: dict[str, Any] | None) -> None:
        await self.session.execute(
            update(Cart)
            .where(Cart.id == cart_id)
            .values(totals_snapshot=snapshot, updated_at=datetime.now(timezone.utc))
        )
        await self._flush()

    async def reset_checkout(self, cart_id: uuid.UUID) -> None:
        await self.session.execute(
            update(Cart)
            .where(Cart.id == cart_id)
            .values(
                coupon_code=None,
                shipping_snapshot=None,
                totals_snapshot=None,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await self._flush()

    async def touch(self, cart_id: uuid.UUID) -> None:
        await self.session.execute(
            update(Cart)
            .where(Cart.id == cart_id)
            .values(updated_at=datetime.now(timezone.utc))
        )
        await self._flush()
