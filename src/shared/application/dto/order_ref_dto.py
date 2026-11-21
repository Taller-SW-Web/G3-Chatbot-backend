"""Insert model for `order_ref` (SPEC-15).

The application builds this plain object; the shared persistence adapter owns
the SQLAlchemy row. It lives in shared because checkout, the outbox worker and
the order reads all use the same local reference table.
"""
import uuid
from dataclasses import dataclass


@dataclass
class NewOrderReference:
    order_id: str
    customer_id: uuid.UUID
    checkout_id: uuid.UUID
    local_status: str
    total: float
