"""Domain entity for checkout (SPEC-12, SPEC-14)."""
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class CheckoutStatus(str, Enum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    PAYMENT_APPROVED = "PAYMENT_APPROVED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


@dataclass
class CheckoutSession:
    id: uuid.UUID
    cart_id: uuid.UUID
    customer_id: uuid.UUID
    status: str
    summary: dict[str, Any]
    total: float
    payment_attempts: int
    expires_at: datetime
    idempotency_key: str
    created_at: datetime
    updated_at: datetime

    @property
    def is_alive(self) -> bool:
        return self.status in (CheckoutStatus.PENDING_PAYMENT.value, CheckoutStatus.PAYMENT_APPROVED.value)

    @property
    def remaining_attempts(self) -> int:
        return max(0, 3 - self.payment_attempts)
