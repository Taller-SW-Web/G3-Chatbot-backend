"""Insert models for post-sale local references (SPEC-19, SPEC-21).

Application code builds these plain objects; the PostgreSQL adapters own the
SQLAlchemy rows. Keeps ORM imports out of `postventa_service`.
"""
import uuid
from dataclasses import dataclass


@dataclass
class NewClaimReference:
    claim_id: str
    code: str
    order_id: str
    customer_id: uuid.UUID
    idempotency_key: str


@dataclass
class NewReturnReference:
    return_id: str
    code: str
    order_id: str
    customer_id: uuid.UUID
    requested_type: str
    idempotency_key: str


@dataclass
class NewEvidenceReference:
    conversation_id: uuid.UUID
    type: str
    url: str
    original_file_name: str
    size_bytes: int
