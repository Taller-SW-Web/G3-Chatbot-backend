"""Checkout and payment use cases (SPEC-12, SPEC-14)."""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from src.adapters.outbound.pagos.payment_simulator_adapter import CardValidationError
from src.adapters.outbound.persistence.checkout_postgres_adapter import CheckoutPostgresAdapter
from src.adapters.outbound.persistence.models.checkout import Checkout
from src.adapters.outbound.persistence.models.payment_attempt import PaymentAttempt
from src.adapters.outbound.persistence.payment_attempt_postgres_adapter import (
    PaymentAttemptPostgresAdapter,
)
from src.domain.entities.checkout import CheckoutStatus
from src.domain.entities.payment_attempt import PaymentRejectReason, PaymentResult
from src.ports.outbound.payment_gateway_port import PaymentGatewayPort


class CheckoutError(Exception):
    def __init__(self, code: str, detail: str = "", extra: dict[str, Any] | None = None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.extra = extra or {}


@dataclass
class CreateCheckoutInput:
    customer_id: uuid.UUID
    idempotency_key: str
    cart_id: uuid.UUID | None = None
    direccion: dict[str, Any] | None = None
    contacto: dict[str, Any] | None = None
    lineas: list[dict[str, Any]] = field(default_factory=list)
    subtotal: float = 0.0
    costo_envio: float = 0.0
    descuento: float = 0.0
    total: float = 0.0


@dataclass
class ProcessPaymentInput:
    checkout_id: uuid.UUID
    customer_id: uuid.UUID
    idempotency_key: str
    numero_tarjeta: str
    titular: str
    vencimiento: str
    cvv: str


class ManageCheckoutUseCase:
    def __init__(
        self,
        checkouts: CheckoutPostgresAdapter,
        payment_attempts: PaymentAttemptPostgresAdapter,
        payment_gateway: PaymentGatewayPort,
        uow: Any | None = None,
        security_client: Any | None = None,
    ):
        self.checkouts = checkouts
        self.payment_attempts = payment_attempts
        self.payment_gateway = payment_gateway
        self.uow = uow
        self.security_client = security_client

    async def create_checkout(self, input_data: CreateCheckoutInput) -> Checkout:
        if not input_data.idempotency_key:
            raise CheckoutError("VALIDACION", "Se requiere Idempotency-Key", {"campo": "Idempotency-Key"})

        cart_id = input_data.cart_id or (uuid.uuid7() if hasattr(uuid, "uuid7") else uuid.uuid4())
        existing = await self.checkouts.obtener_vivo_por_cart(cart_id)
        if existing and existing.idempotency_key == input_data.idempotency_key:
            return existing

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=15)

        summary = {
            "lineas": input_data.lineas,
            "subtotal": input_data.subtotal,
            "costoEnvio": input_data.costo_envio,
            "descuento": input_data.descuento,
            "total": input_data.total,
            "direccion": input_data.direccion or {},
            "contacto": input_data.contacto or {},
        }

        checkout_row = Checkout(
            cart_id=cart_id,
            customer_id=input_data.customer_id,
            status=CheckoutStatus.PENDING_PAYMENT.value,
            summary=summary,
            total=input_data.total,
            payment_attempts=0,
            expires_at=expires_at,
            idempotency_key=input_data.idempotency_key,
        )

        saved = await self.checkouts.guardar(checkout_row)
        if self.uow:
            await self.uow.commit()
        return saved

    async def get_checkout(self, checkout_id: uuid.UUID, customer_id: uuid.UUID) -> Checkout:
        row = await self.checkouts.obtener_por_id(checkout_id)
        if not row or row.customer_id != customer_id:
            raise CheckoutError("RECURSO_NO_ENCONTRADO", "Checkout no encontrado")

        now = datetime.now(timezone.utc)
        if row.status == CheckoutStatus.PENDING_PAYMENT.value and now > row.expires_at:
            await self.checkouts.marcar_estado(row.id, CheckoutStatus.EXPIRED.value)
            if self.uow:
                await self.uow.commit()
            row.status = CheckoutStatus.EXPIRED.value

        return row

    async def process_payment(self, input_data: ProcessPaymentInput) -> PaymentAttempt:
        if not input_data.idempotency_key:
            raise CheckoutError("VALIDACION", "Se requiere Idempotency-Key", {"campo": "Idempotency-Key"})

        row = await self.checkouts.obtener_por_id(input_data.checkout_id)
        if not row or row.customer_id != input_data.customer_id:
            raise CheckoutError("RECURSO_NO_ENCONTRADO", "Checkout no encontrado")

        now = datetime.now(timezone.utc)
        if row.status == CheckoutStatus.EXPIRED.value or (
            row.status == CheckoutStatus.PENDING_PAYMENT.value and now > row.expires_at
        ):
            if row.status != CheckoutStatus.EXPIRED.value:
                await self.checkouts.marcar_estado(row.id, CheckoutStatus.EXPIRED.value)
                if self.uow:
                    await self.uow.commit()
            raise CheckoutError("CHECKOUT_EXPIRADO", "El checkout venció hace más de 15 minutos")

        if row.status == CheckoutStatus.PAYMENT_APPROVED.value:
            attempts = await self.payment_attempts.listar_por_checkout(row.id)
            approved = next((a for a in attempts if a.result == PaymentResult.APPROVED.value), None)
            if approved:
                return approved

        if row.status == CheckoutStatus.FAILED.value or (row.payment_attempts or 0) >= 3:
            raise CheckoutError(
                "PAGO_RECHAZADO",
                "Se agotaron los intentos de pago permitidos",
                {"motivo": "INTENTOS_AGOTADOS", "intentosRestantes": 0},
            )

        try:
            payment_res = await self.payment_gateway.process_payment(
                card_number=input_data.numero_tarjeta,
                holder_name=input_data.titular,
                expiry=input_data.vencimiento,
                cvv=input_data.cvv,
                amount=float(row.total),
            )
        except CardValidationError as exc:
            raise CheckoutError("VALIDACION", exc.message, {"campo": exc.field}) from exc

        if self.security_client and hasattr(self.security_client, "introspect"):
            try:
                intro = await self.security_client.introspect(str(input_data.customer_id))
                if intro and not intro.get("active", True):
                    raise CheckoutError("TOKEN_INVALIDO", "Tu sesión ya no es válida, vuelve a iniciar sesión")
            except Exception as e:
                if isinstance(e, CheckoutError):
                    raise

        if payment_res.result == PaymentResult.APPROVED.value:
            await self.checkouts.marcar_estado(row.id, CheckoutStatus.PAYMENT_APPROVED.value)
            attempt = PaymentAttempt(
                checkout_id=row.id,
                idempotency_key=input_data.idempotency_key,
                result=PaymentResult.APPROVED.value,
                brand=payment_res.brand,
                last4=payment_res.last4,
                amount=row.total,
                transaction_id=payment_res.transaction_id,
            )
            saved_attempt = await self.payment_attempts.guardar(attempt)
            if self.uow:
                await self.uow.commit()
            return saved_attempt

        await self.checkouts.incrementar_intentos(row.id)
        current_attempts = (row.payment_attempts or 0) + 1
        remaining = max(0, 3 - current_attempts)

        if current_attempts >= 3:
            await self.checkouts.marcar_estado(row.id, CheckoutStatus.FAILED.value)

        attempt = PaymentAttempt(
            checkout_id=row.id,
            idempotency_key=input_data.idempotency_key,
            result=payment_res.result,
            brand=payment_res.brand,
            last4=payment_res.last4,
            amount=row.total,
            reason=payment_res.reason,
        )
        await self.payment_attempts.guardar(attempt)
        if self.uow:
            await self.uow.commit()

        motivo_map = {
            PaymentRejectReason.INSUFFICIENT_FUNDS.value: "FONDOS_INSUFICIENTES",
            PaymentRejectReason.DECLINED_BY_ISSUER.value: "RECHAZADO_POR_EMISOR",
            PaymentRejectReason.PROCESSING_ERROR.value: "ERROR_PROCESAMIENTO",
        }
        motivo_es = motivo_map.get(payment_res.reason or "", payment_res.reason or "RECHAZADO")

        if payment_res.result == PaymentResult.ERROR.value:
            raise CheckoutError(
                "ERROR_PROCESAMIENTO",
                "Ocurrió un error temporal al procesar el pago. Intente de nuevo.",
                {"motivo": motivo_es, "intentosRestantes": remaining},
            )

        raise CheckoutError(
            "PAGO_RECHAZADO",
            "Tu tarjeta fue rechazada. Puedes intentar con otra tarjeta.",
            {"motivo": motivo_es, "intentosRestantes": remaining},
        )
