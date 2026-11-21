"""Adapter de valoración de la compra (SPEC-24)."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.mod_checkout.domain.purchase_rating import (
    NewPurchaseRating,
    PurchaseRatingAlreadyExists,
)
from src.shared.infrastructure.db.models.feedback import PurchaseRating


class PurchaseRatingPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def obtener_por_order(self, order_id: str) -> PurchaseRating | None:
        stmt = select(PurchaseRating).where(PurchaseRating.order_id == order_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def guardar(self, row: NewPurchaseRating | PurchaseRating) -> PurchaseRating:
        if not isinstance(row, PurchaseRating):
            row = PurchaseRating(
                order_id=row.order_id,
                customer_id=row.customer_id,
                rating=row.rating,
                comment=row.comment,
                created_at=datetime.now(timezone.utc),
            )
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise PurchaseRatingAlreadyExists(row.order_id) from exc
        return row
