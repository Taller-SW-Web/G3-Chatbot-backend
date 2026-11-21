"""Insert models for customer feedback (SPEC-24)."""
import uuid
from dataclasses import dataclass


@dataclass
class NewFeedbackSuggestion:
    customer_id: uuid.UUID
    type: str
    title: str
    description: str
    contact_email: str
    screenshot_key: str | None
    idempotency_key: str
