"""Outbound port: payment processing (SPEC-14)."""
from dataclasses import dataclass
from typing import Protocol


class CardValidationError(Exception):
    """Invalid card data rejected before the gateway runs (SPEC-14).

    Lives in `ports/` so `application/` can translate it to a `VALIDACION`
    problem without importing a concrete adapter.
    """

    def __init__(self, message: str, field: str = "tarjeta"):
        super().__init__(message)
        self.message = message
        self.field = field


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
