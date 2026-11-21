"""Read port: the paid-order snapshot owned by `mod-checkout` (ADR-0024).

`mod-orders` builds the confirmation e-mail from `order_ref`, the checkout
`summary` and the approved `payment_attempt`. Those tables belong to checkout,
so orders consumes this port instead of importing checkout's ORM models or
querying its tables directly.
"""
from typing import Protocol

from src.shared.application.ports.notification_repository_port import ConfirmationSource


class CheckoutSnapshotPort(Protocol):
    async def cargar(self, order_id: str) -> ConfirmationSource | None:
        """Paid-order snapshot, or None when the order/checkout does not exist."""
        ...
