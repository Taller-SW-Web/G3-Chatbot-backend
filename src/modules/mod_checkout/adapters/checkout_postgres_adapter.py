"""Adapter checkout (SPEC-12, SPEC-14).

Operaciones minimas derivadas de modelo-datos.md (seccion `checkout`).
No hace commit: la transaccion la controla el caso de uso. La creacion
del checkout, el intento de pago y la tarea outbox comparten transaccion
(patron transaccional, ADR-0011).
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.mod_checkout.domain.checkout import NewCheckout
from src.shared.infrastructure.db.models.checkout import Checkout

MAX_PAYMENT_ATTEMPTS = 3


class CheckoutPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def guardar(self, row: Checkout | NewCheckout) -> Checkout:
        if not isinstance(row, Checkout):
            row = Checkout(
                id=row.id,
                cart_id=row.cart_id,
                customer_id=row.customer_id,
                status=row.status,
                summary=row.summary,
                total=row.total,
                payment_attempts=row.payment_attempts,
                expires_at=row.expires_at,
                idempotency_key=row.idempotency_key,
            )
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError:
            # UNIQUE parcial (cart_id vivo) o idempotency_key: el checkout
            # ya existe; se devuelve el existente solo si pertenece al mismo customer.
            await self.session.rollback()
            existing = await self.obtener_por_idempotency_key(row.idempotency_key)
            if existing is None:
                existing = await self.obtener_vivo_por_cart(row.cart_id, customer_id=row.customer_id)
            if existing is None or existing.customer_id != row.customer_id:
                raise
            return existing
        return row

    async def obtener_por_id(self, id: uuid.UUID, *, for_update: bool = False) -> Checkout | None:
        """`for_update` takes a row lock (SELECT ... FOR UPDATE) until the unit of work ends."""
        stmt = select(Checkout).where(Checkout.id == id)
        if for_update:
            # populate_existing: a waiter must see what the lock holder committed.
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def obtener_por_idempotency_key(self, idempotency_key: str) -> Checkout | None:
        stmt = select(Checkout).where(Checkout.idempotency_key == idempotency_key)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def obtener_vivo_por_cart(
        self, cart_id: uuid.UUID, customer_id: uuid.UUID | None = None
    ) -> Checkout | None:
        # Un solo checkout vivo por carrito (anti doble-clic).
        conditions = [
            Checkout.cart_id == cart_id,
            Checkout.status.in_(["PENDING_PAYMENT", "PAYMENT_APPROVED"]),
        ]
        if customer_id is not None:
            conditions.append(Checkout.customer_id == customer_id)
        stmt = (
            select(Checkout)
            .where(*conditions)
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
        """Atomic `payment_attempts + 1`, capped at 3; None when the cap was already reached."""
        res = await self.session.execute(
            update(Checkout)
            .where(Checkout.id == id, Checkout.payment_attempts < MAX_PAYMENT_ATTEMPTS)
            .values(payment_attempts=Checkout.payment_attempts + 1, updated_at=datetime.now(timezone.utc))
            .returning(Checkout.id)
            .execution_options(synchronize_session=False)
        )
        if res.scalar_one_or_none() is None:
            return None
        return await self.obtener_por_id_fresco(id)

    async def obtener_por_id_fresco(self, id: uuid.UUID) -> Checkout | None:
        stmt = select(Checkout).where(Checkout.id == id).execution_options(populate_existing=True)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def latest_document(self, customer_id: uuid.UUID) -> dict[str, str] | None:
        """`summary.contacto` of the most recent checkout of the customer (SPEC-03 profile).

        Returns `{"tipo", "numero"}`; the full number never leaves the order
        snapshot (modelo-datos.md §AccountPage, RN-CHK-02).
        """
        stmt = (
            select(Checkout)
            .where(Checkout.customer_id == customer_id)
            .order_by(Checkout.created_at.desc())
            .limit(1)
        )
        res = await self.session.execute(stmt)
        row = res.scalar_one_or_none()
        if row is None or not isinstance(row.summary, dict):
            return None
        contacto = row.summary.get("contacto")
        if not isinstance(contacto, dict) or not contacto.get("numeroDocumento"):
            return None
        return {
            "tipo": str(contacto.get("tipoDocumento", "")),
            "numero": str(contacto["numeroDocumento"]),
        }
