"""Payment simulator adapter (SPEC-14 Req. 3 and Req. 5).

Deterministic test cards simulation table for academic demos and tests.
Does not persist card numbers or sensitive data.
"""
import asyncio
from datetime import datetime, timezone
import re
import uuid

from src.domain.entities.payment_attempt import CardBrand, PaymentRejectReason, PaymentResult
from src.ports.outbound.payment_gateway_port import PaymentGatewayPort, PaymentProcessResult


class CardValidationError(Exception):
    def __init__(self, message: str, field: str = "tarjeta"):
        super().__init__(message)
        self.message = message
        self.field = field


def clean_digits(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


def validate_luhn(digits: str) -> bool:
    if not digits or len(digits) < 13:
        return False
    checksum = 0
    reverse_digits = [int(d) for d in digits[::-1]]
    for i, d in enumerate(reverse_digits):
        if i % 2 == 1:
            doubled = d * 2
            checksum += doubled - 9 if doubled > 9 else doubled
        else:
            checksum += d
    return checksum % 10 == 0


def detect_brand(digits: str) -> CardBrand | None:
    if digits.startswith("4") and len(digits) in (13, 16, 19):
        return CardBrand.VISA
    if len(digits) == 16:
        try:
            prefix2 = int(digits[:2])
            prefix4 = int(digits[:4])
            if 51 <= prefix2 <= 55 or 2221 <= prefix4 <= 2720:
                return CardBrand.MASTERCARD
        except ValueError:
            pass
    if len(digits) == 15 and (digits.startswith("34") or digits.startswith("37")):
        return CardBrand.AMEX
    return None


def validate_expiry(expiry_str: str) -> None:
    clean = expiry_str.strip()
    match = re.match(r"^(\d{1,2})/(\d{2}|\d{4})$", clean)
    if not match:
        raise CardValidationError("Fecha de vencimiento inválida (debe ser MM/AA)", field="vencimiento")
    month, year_s = int(match.group(1)), match.group(2)
    if month < 1 or month > 12:
        raise CardValidationError("Mes de vencimiento inválido", field="vencimiento")

    now = datetime.now(timezone.utc)
    current_year = now.year
    current_month = now.month

    year = int(year_s)
    if len(year_s) == 2:
        year += 2000

    if year < current_year or (year == current_year and month < current_month):
        raise CardValidationError("La tarjeta está vencida", field="vencimiento")


def validate_cvv(cvv: str, brand: CardBrand) -> None:
    clean = cvv.strip()
    expected_len = 4 if brand == CardBrand.AMEX else 3
    if not clean.isdigit() or len(clean) != expected_len:
        raise CardValidationError(
            f"CVV debe tener {expected_len} dígitos para {brand.value}", field="cvv"
        )


class PaymentSimulatorAdapter(PaymentGatewayPort):
    def __init__(self, simulate_latency: bool = False):
        self._simulate_latency = simulate_latency

    def validate_card(
        self,
        card_number: str,
        holder_name: str,
        expiry: str,
        cvv: str,
    ) -> tuple[str, CardBrand]:
        digits = clean_digits(card_number)
        if not digits:
            raise CardValidationError("Número de tarjeta requerido", field="tarjeta")

        brand = detect_brand(digits)
        if not brand:
            raise CardValidationError("Marca de tarjeta no soportada", field="tarjeta")

        if not validate_luhn(digits):
            raise CardValidationError("Número de tarjeta inválido (Luhn)", field="tarjeta")

        if not holder_name or len(holder_name.strip()) < 3:
            raise CardValidationError("Nombre del titular inválido", field="titular")

        validate_expiry(expiry)
        validate_cvv(cvv, brand)

        return digits, brand

    async def process_payment(
        self,
        card_number: str,
        holder_name: str,
        expiry: str,
        cvv: str,
        amount: float,
    ) -> PaymentProcessResult:
        digits, brand = self.validate_card(card_number, holder_name, expiry, cvv)
        last4 = digits[-4:]

        # Deterministic simulation table (SPEC-14 Requisito 5)
        if digits == "4000000000000002":
            return PaymentProcessResult(
                result=PaymentResult.REJECTED.value,
                brand=brand.value,
                last4=last4,
                amount=amount,
                reason=PaymentRejectReason.INSUFFICIENT_FUNDS.value,
            )

        if digits == "4000000000000069":
            return PaymentProcessResult(
                result=PaymentResult.REJECTED.value,
                brand=brand.value,
                last4=last4,
                amount=amount,
                reason=PaymentRejectReason.DECLINED_BY_ISSUER.value,
            )

        if digits == "4000000000000119":
            return PaymentProcessResult(
                result=PaymentResult.ERROR.value,
                brand=brand.value,
                last4=last4,
                amount=amount,
                reason=PaymentRejectReason.PROCESSING_ERROR.value,
            )

        if digits == "4000000000003220":
            if self._simulate_latency:
                await asyncio.sleep(5)
            return PaymentProcessResult(
                result=PaymentResult.APPROVED.value,
                brand=brand.value,
                last4=last4,
                amount=amount,
                transaction_id=f"SIM-{uuid.uuid4()}",
            )

        # Default approved for all other valid cards (including 4111..., 5555..., 3782...)
        return PaymentProcessResult(
            result=PaymentResult.APPROVED.value,
            brand=brand.value,
            last4=last4,
            amount=amount,
            transaction_id=f"SIM-{uuid.uuid4()}",
        )
