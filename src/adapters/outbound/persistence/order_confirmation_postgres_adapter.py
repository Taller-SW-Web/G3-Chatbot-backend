"""Snapshot of a paid order for the confirmation e-mail (SPEC-16).

Read-only join of `order_ref` (owner, local status), its `checkout` (the
`summary` sent to Ventas) and the last APPROVED `payment_attempt` (card
brand and last 4 digits, never the PAN).
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models.checkout import Checkout
from src.adapters.outbound.persistence.models.order_ref import OrderRef
from src.adapters.outbound.persistence.models.payment_attempt import PaymentAttempt
from src.ports.outbound.notification_repository_port import ConfirmationSource


class OrderConfirmationPostgresAdapter:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def cargar(self, order_id: str) -> ConfirmationSource | None:
        stmt = (
            select(OrderRef, Checkout)
            .join(Checkout, Checkout.id == OrderRef.checkout_id)
            .where(OrderRef.order_id == order_id)
        )
        found = (await self.session.execute(stmt)).first()
        if found is None:
            return None
        order, checkout = found
        attempt_stmt = (
            select(PaymentAttempt)
            .where(PaymentAttempt.checkout_id == checkout.id, PaymentAttempt.result == "APPROVED")
            .order_by(PaymentAttempt.created_at.desc())
            .limit(1)
        )
        attempt = (await self.session.execute(attempt_stmt)).scalar_one_or_none()
        return ConfirmationSource(
            order_id=order.order_id,
            customer_id=order.customer_id,
            local_status=order.local_status,
            created_at=order.created_at,
            summary=dict(checkout.summary or {}),
            card_brand=attempt.brand if attempt else None,
            card_last4=attempt.last4 if attempt else None,
        )
