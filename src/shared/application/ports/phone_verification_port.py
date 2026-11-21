"""Outbound port: local phone verification store (SPEC-04, used by SPEC-03 profile)."""
import uuid
from typing import Protocol


class PhoneVerificationPort(Protocol):
    async def existe(self, customer_id: uuid.UUID, phone: str) -> bool:
        """True when this customer already verified this exact, current phone number."""
        ...
