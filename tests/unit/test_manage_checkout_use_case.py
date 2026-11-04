"""Unit tests for checkout and payment processing use case (SPEC-14)."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.outbound.pagos.payment_simulator_adapter import PaymentSimulatorAdapter
from src.adapters.outbound.persistence.models.checkout import Checkout
from src.application.use_cases.manage_checkout_use_case import (
    CheckoutError,
    CreateCheckoutInput,
    ManageCheckoutUseCase,
    ProcessPaymentInput,
)
from src.domain.entities.checkout import CheckoutStatus
from src.domain.entities.payment_attempt import PaymentResult


@pytest.fixture
def mock_checkouts():
    adapter = MagicMock()
    adapter.guardar = AsyncMock(side_effect=lambda row: row)
    adapter.obtener_por_id = AsyncMock(return_value=None)
    adapter.obtener_vivo_por_cart = AsyncMock(return_value=None)
    adapter.marcar_estado = AsyncMock()
    adapter.incrementar_intentos = AsyncMock()
    return adapter


@pytest.fixture
def mock_attempts():
    adapter = MagicMock()
    adapter.guardar = AsyncMock(side_effect=lambda row: row)
    adapter.listar_por_checkout = AsyncMock(return_value=[])
    adapter.contar_por_checkout = AsyncMock(return_value=0)
    return adapter


@pytest.fixture
def payment_gateway():
    return PaymentSimulatorAdapter()


@pytest.fixture
def use_case(mock_checkouts, mock_attempts, payment_gateway):
    uow = MagicMock()
    uow.commit = AsyncMock()
    uow.rollback = AsyncMock()
    return ManageCheckoutUseCase(
        checkouts=mock_checkouts,
        payment_attempts=mock_attempts,
        payment_gateway=payment_gateway,
        uow=uow,
    )


@pytest.mark.asyncio
async def test_create_checkout_requires_idempotency_key(use_case):
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.create_checkout(
            CreateCheckoutInput(
                customer_id=uuid.uuid4(),
                idempotency_key="",
                total=100.0,
            )
        )
    assert exc_info.value.code == "VALIDACION"


@pytest.mark.asyncio
async def test_create_checkout_success_with_15_min_expiry(use_case, mock_checkouts):
    cust_id = uuid.uuid4()
    cart_id = uuid.uuid4()
    before = datetime.now(timezone.utc)

    checkout = await use_case.create_checkout(
        CreateCheckoutInput(
            customer_id=cust_id,
            idempotency_key="idemp-chk-1",
            cart_id=cart_id,
            lineas=[{"sku": "SKU-1", "cantidad": 2, "precio": 50.0}],
            subtotal=100.0,
            costo_envio=15.0,
            descuento=10.0,
            total=105.0,
        )
    )

    assert checkout.customer_id == cust_id
    assert checkout.cart_id == cart_id
    assert checkout.status == CheckoutStatus.PENDING_PAYMENT.value
    assert checkout.total == 105.0
    assert checkout.payment_attempts == 0
    assert checkout.expires_at >= before + timedelta(minutes=14, seconds=55)


@pytest.mark.asyncio
async def test_create_checkout_idempotent_returns_existing(use_case, mock_checkouts):
    cart_id = uuid.uuid4()
    existing = Checkout(
        id=uuid.uuid4(),
        cart_id=cart_id,
        customer_id=uuid.uuid4(),
        status=CheckoutStatus.PENDING_PAYMENT.value,
        summary={},
        total=100.0,
        payment_attempts=0,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        idempotency_key="same-key",
    )
    mock_checkouts.obtener_vivo_por_cart.return_value = existing

    res = await use_case.create_checkout(
        CreateCheckoutInput(
            customer_id=existing.customer_id,
            idempotency_key="same-key",
            cart_id=cart_id,
            total=100.0,
        )
    )
    assert res == existing


@pytest.mark.asyncio
async def test_get_checkout_rejects_foreign_customer(use_case, mock_checkouts):
    checkout_id = uuid.uuid4()
    mock_checkouts.obtener_por_id.return_value = Checkout(
        id=checkout_id,
        cart_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        status="PENDING_PAYMENT",
        summary={},
        total=100.0,
        payment_attempts=0,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        idempotency_key="k1",
    )

    with pytest.raises(CheckoutError) as exc_info:
        await use_case.get_checkout(checkout_id, customer_id=uuid.uuid4())
    assert exc_info.value.code == "RECURSO_NO_ENCONTRADO"


@pytest.mark.asyncio
async def test_payment_rejected_when_checkout_expired(use_case, mock_checkouts):
    checkout_id = uuid.uuid4()
    cust_id = uuid.uuid4()
    mock_checkouts.obtener_por_id.return_value = Checkout(
        id=checkout_id,
        cart_id=uuid.uuid4(),
        customer_id=cust_id,
        status="PENDING_PAYMENT",
        summary={},
        total=100.0,
        payment_attempts=0,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        idempotency_key="k1",
    )

    with pytest.raises(CheckoutError) as exc_info:
        await use_case.process_payment(
            ProcessPaymentInput(
                checkout_id=checkout_id,
                customer_id=cust_id,
                idempotency_key="pay-key-1",
                numero_tarjeta="4111111111111111",
                titular="Diego Test",
                vencimiento="12/28",
                cvv="123",
            )
        )
    assert exc_info.value.code == "CHECKOUT_EXPIRADO"


@pytest.mark.asyncio
async def test_payment_invalid_card_raises_validacion_without_counting_attempt(
    use_case, mock_checkouts
):
    checkout_id = uuid.uuid4()
    cust_id = uuid.uuid4()
    mock_checkouts.obtener_por_id.return_value = Checkout(
        id=checkout_id,
        cart_id=uuid.uuid4(),
        customer_id=cust_id,
        status="PENDING_PAYMENT",
        summary={},
        total=100.0,
        payment_attempts=0,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        idempotency_key="k1",
    )

    with pytest.raises(CheckoutError) as exc_info:
        await use_case.process_payment(
            ProcessPaymentInput(
                checkout_id=checkout_id,
                customer_id=cust_id,
                idempotency_key="pay-key-1",
                numero_tarjeta="1234",  # invalid
                titular="Diego",
                vencimiento="12/28",
                cvv="123",
            )
        )
    assert exc_info.value.code == "VALIDACION"
    mock_checkouts.incrementar_intentos.assert_not_called()


@pytest.mark.asyncio
async def test_payment_approved_deterministic_test_card(
    use_case, mock_checkouts, mock_attempts
):
    checkout_id = uuid.uuid4()
    cust_id = uuid.uuid4()
    row = Checkout(
        id=checkout_id,
        cart_id=uuid.uuid4(),
        customer_id=cust_id,
        status="PENDING_PAYMENT",
        summary={},
        total=150.0,
        payment_attempts=0,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        idempotency_key="k1",
    )
    mock_checkouts.obtener_por_id.return_value = row

    attempt = await use_case.process_payment(
        ProcessPaymentInput(
            checkout_id=checkout_id,
            customer_id=cust_id,
            idempotency_key="pay-key-ok",
            numero_tarjeta="4111 1111 1111 1111",
            titular="Diego Valido",
            vencimiento="10/29",
            cvv="456",
        )
    )

    assert attempt.result == PaymentResult.APPROVED.value
    assert attempt.brand == "VISA"
    assert attempt.last4 == "1111"
    assert attempt.transaction_id.startswith("SIM-")
    assert attempt.reason is None
    mock_checkouts.marcar_estado.assert_called_with(checkout_id, CheckoutStatus.PAYMENT_APPROVED.value)


@pytest.mark.asyncio
async def test_payment_rejected_insufficient_funds_decrements_remaining(
    use_case, mock_checkouts, mock_attempts
):
    checkout_id = uuid.uuid4()
    cust_id = uuid.uuid4()
    row = Checkout(
        id=checkout_id,
        cart_id=uuid.uuid4(),
        customer_id=cust_id,
        status="PENDING_PAYMENT",
        summary={},
        total=200.0,
        payment_attempts=0,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        idempotency_key="k1",
    )
    mock_checkouts.obtener_por_id.return_value = row

    with pytest.raises(CheckoutError) as exc_info:
        await use_case.process_payment(
            ProcessPaymentInput(
                checkout_id=checkout_id,
                customer_id=cust_id,
                idempotency_key="pay-key-rej",
                numero_tarjeta="4000 0000 0000 0002",
                titular="Diego Fondos",
                vencimiento="10/29",
                cvv="123",
            )
        )

    assert exc_info.value.code == "PAGO_RECHAZADO"
    assert exc_info.value.extra["motivo"] == "FONDOS_INSUFICIENTES"
    assert exc_info.value.extra["intentosRestantes"] == 2
    mock_checkouts.incrementar_intentos.assert_called_with(checkout_id)


@pytest.mark.asyncio
async def test_third_failed_attempt_marks_checkout_failed(
    use_case, mock_checkouts, mock_attempts
):
    checkout_id = uuid.uuid4()
    cust_id = uuid.uuid4()
    row = Checkout(
        id=checkout_id,
        cart_id=uuid.uuid4(),
        customer_id=cust_id,
        status="PENDING_PAYMENT",
        summary={},
        total=200.0,
        payment_attempts=2,  # already 2 failed
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        idempotency_key="k1",
    )
    mock_checkouts.obtener_por_id.return_value = row

    with pytest.raises(CheckoutError) as exc_info:
        await use_case.process_payment(
            ProcessPaymentInput(
                checkout_id=checkout_id,
                customer_id=cust_id,
                idempotency_key="pay-key-rej3",
                numero_tarjeta="4000 0000 0000 0069",
                titular="Diego Bloqueado",
                vencimiento="10/29",
                cvv="123",
            )
        )

    assert exc_info.value.code == "PAGO_RECHAZADO"
    assert exc_info.value.extra["intentosRestantes"] == 0
    mock_checkouts.marcar_estado.assert_called_with(checkout_id, CheckoutStatus.FAILED.value)
