"""Insert model for the transactional outbox (SPEC-15, ADR-0011).

Application code builds this plain object; the persistence adapter owns the
SQLAlchemy row. Keeps ORM imports out of use cases.
"""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NewOutboxTask:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "PENDING"
    attempts: int = 0
