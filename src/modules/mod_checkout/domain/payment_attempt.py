"""Domain entity for payment attempt (SPEC-14)."""
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class PaymentResult(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class PaymentRejectReason(str, Enum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    DECLINED_BY_ISSUER = "DECLINED_BY_ISSUER"
    PROCESSING_ERROR = "PROCESSING_ERROR"
    INVALID_DATA = "INVALID_DATA"


class CardBrand(str, Enum):
    VISA = "VISA"
    MASTERCARD = "MASTERCARD"
    AMEX = "AMEX"


@dataclass
class PaymentAttemptEntity:
    id: uuid.UUID
    checkout_id: uuid.UUID
    idempotency_key: str
    result: str
    brand: str
    last4: str
    amount: float
    created_at: datetime
    reason: str | None = None
    transaction_id: str | None = None
