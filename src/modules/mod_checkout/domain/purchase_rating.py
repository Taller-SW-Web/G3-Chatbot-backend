"""Insert model for `purchase_rating` (SPEC-24)."""
import uuid
from dataclasses import dataclass


@dataclass
class NewPurchaseRating:
    order_id: str
    customer_id: uuid.UUID
    rating: int
    comment: str | None


class PurchaseRatingAlreadyExists(Exception):
    """Unique `order_id`: the order already has a rating."""

