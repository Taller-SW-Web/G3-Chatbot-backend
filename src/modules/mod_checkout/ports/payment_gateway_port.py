"""Outbound port: payment processing (SPEC-14)."""
from dataclasses import dataclass
from typing import Protocol


@dataclass
class PaymentProcessResult:
    result: str
    brand: str
    last4: str
    amount: float
    transaction_id: str | None = None
    reason: str | None = None


class PaymentGatewayPort(Protocol):
    async def process_payment(
        self,
        card_number: str,
        holder_name: str,
        expiry: str,
        cvv: str,
        amount: float,
    ) -> PaymentProcessResult:
        """Processes payment using simulation table or payment gateway."""
        ...
