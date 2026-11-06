"""Unit tests for checkout and payment processing use case (SPEC-14)."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.outbound.pagos.payment_simulator_adapter import PaymentSimulatorAdapter
from src.adapters.outbound.persistence.models.checkout import Checkout
from src.adapters.outbound.persistence.models.payment_attempt import PaymentAttempt
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


@pytest.mark.asyncio
async def test_introspection_invalid_token_raises_token_invalido_401(
    mock_checkouts, mock_attempts, payment_gateway
):
    security = MagicMock()
    security.introspect = AsyncMock(return_value={"activo": False, "motivo": "USUARIO_INACTIVO"})
    uow = MagicMock()
    uow.commit = AsyncMock()

    use_case = ManageCheckoutUseCase(
        checkouts=mock_checkouts,
        payment_attempts=mock_attempts,
        payment_gateway=payment_gateway,
        uow=uow,
        security_client=security,
    )

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
                idempotency_key="pay-intro-1",
                numero_tarjeta="4111 1111 1111 1111",
                titular="Diego Test",
                vencimiento="12/28",
                cvv="123",
                access_token="token-bloqueado",
            )
        )
    assert exc_info.value.code == "TOKEN_INVALIDO"


@pytest.mark.asyncio
async def test_introspection_down_fails_closed_with_503(
    mock_checkouts, mock_attempts, payment_gateway
):
    security = MagicMock()
    security.introspect = AsyncMock(side_effect=TimeoutError("Seguridad unreachable"))
    uow = MagicMock()
    uow.commit = AsyncMock()

    use_case = ManageCheckoutUseCase(
        checkouts=mock_checkouts,
        payment_attempts=mock_attempts,
        payment_gateway=payment_gateway,
        uow=uow,
        security_client=security,
    )

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
                idempotency_key="pay-intro-timeout",
                numero_tarjeta="4111 1111 1111 1111",
                titular="Diego Test",
                vencimiento="12/28",
                cvv="123",
                access_token="token-real",
            )
        )
    assert exc_info.value.code == "SERVICIO_NO_DISPONIBLE"


@pytest.mark.asyncio
async def test_payment_idempotency_key_returns_existing_attempt(
    mock_checkouts, mock_attempts, payment_gateway
):
    checkout_id = uuid.uuid4()
    cust_id = uuid.uuid4()
    existing_attempt = PaymentAttempt(
        id=uuid.uuid4(),
        checkout_id=checkout_id,
        idempotency_key="pay-idemp-key-dup",
        result="APPROVED",
        brand="VISA",
        last4="1111",
        amount=150.0,
        transaction_id="SIM-previo-1",
    )
    mock_attempts.obtener_por_idempotency_key = AsyncMock(return_value=existing_attempt)

    uow = MagicMock()
    uow.commit = AsyncMock()
    use_case = ManageCheckoutUseCase(
        checkouts=mock_checkouts,
        payment_attempts=mock_attempts,
        payment_gateway=payment_gateway,
        uow=uow,
    )

    res = await use_case.process_payment(
        ProcessPaymentInput(
            checkout_id=checkout_id,
            customer_id=cust_id,
            idempotency_key="pay-idemp-key-dup",
            numero_tarjeta="4111 1111 1111 1111",
            titular="Diego Test",
            vencimiento="12/28",
            cvv="123",
        )
    )
    assert res == existing_attempt


@pytest.mark.asyncio
async def test_create_checkout_recalculates_items_and_shipping_from_server(
    mock_checkouts, mock_attempts, payment_gateway
):
    cust_id = uuid.uuid4()
    cart_id = uuid.uuid4()

    mock_cart = MagicMock()
    mock_cart.id = cart_id
    mock_cart.customer_id = cust_id

    mock_carts = MagicMock()
    mock_carts.obtener_abierto_por_cliente = AsyncMock(return_value=mock_cart)
    mock_carts.cambiar_estado = AsyncMock()

    mock_item = MagicMock()
    mock_item.sku = "SKU-PROMO-1"
    mock_item.name = "Polo Algodon"
    mock_item.quantity = 2
    mock_item.unit_price_ref = 40.0
    mock_item.image_url = None

    mock_items = MagicMock()
    mock_items.listar_por_cart = AsyncMock(return_value=[mock_item])

    uow = MagicMock()
    uow.commit = AsyncMock()

    use_case = ManageCheckoutUseCase(
        checkouts=mock_checkouts,
        payment_attempts=mock_attempts,
        payment_gateway=payment_gateway,
        uow=uow,
        carts=mock_carts,
        cart_items=mock_items,
    )

    # 2 * 40 = 80 subtotal. Envio 12.50. Total servidor = 92.50
    checkout = await use_case.create_checkout(
        CreateCheckoutInput(
            customer_id=cust_id,
            idempotency_key="chk-recalc-1",
            cart_id=None,  # Not provided, resolves open cart!
            costo_envio=12.50,
            total=92.50,
        )
    )
    assert checkout.total == 92.50
    assert checkout.summary["subtotal"] == 80.0
    assert checkout.summary["costoEnvio"] == 12.50
    assert len(checkout.summary["lineas"]) == 1
    mock_carts.cambiar_estado.assert_called_with(cart_id, "IN_CHECKOUT")


@pytest.mark.asyncio
async def test_card_number_never_persisted_in_attempt(use_case, mock_checkouts, mock_attempts):
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

    pan = "4111 1111 1111 1111"
    attempt = await use_case.process_payment(
        ProcessPaymentInput(
            checkout_id=checkout_id,
            customer_id=cust_id,
            idempotency_key="pay-pan-check",
            numero_tarjeta=pan,
            titular="Diego Test",
            vencimiento="12/28",
            cvv="123",
        )
    )

    assert not hasattr(attempt, "numero_tarjeta")
    assert not hasattr(attempt, "cvv")
    assert not hasattr(attempt, "pan")
    assert attempt.last4 == "1111"
    assert attempt.brand == "VISA"


@pytest.mark.asyncio
async def test_payment_approved_marks_order_paid_notified_and_enqueues_confirmation(
    mock_checkouts, mock_attempts, payment_gateway
):
    checkout_id = uuid.uuid4()
    cust_id = uuid.uuid4()
    order_id = "PED-2026-00891"

    row = Checkout(
        id=checkout_id,
        cart_id=uuid.uuid4(),
        customer_id=cust_id,
        status="PENDING_PAYMENT",
        summary={"orderId": order_id},
        total=150.0,
        payment_attempts=0,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        idempotency_key="k1",
    )
    mock_checkouts.obtener_por_id.return_value = row

    mock_orders = MagicMock()
    order_ref_obj = MagicMock()
    order_ref_obj.order_id = order_id
    mock_orders.obtener_por_checkout_id = AsyncMock(return_value=order_ref_obj)
    mock_orders.actualizar_estado = AsyncMock()

    mock_notif_service = MagicMock()
    mock_notif_service.enqueue_confirmation = AsyncMock(
        return_value={
            "correoEnmascarado": "m***a@ejemplo.com",
            "mensaje": "Te enviaremos la confirmación a m***a@…",
        }
    )

    uow = MagicMock()
    uow.commit = AsyncMock()

    use_case = ManageCheckoutUseCase(
        checkouts=mock_checkouts,
        payment_attempts=mock_attempts,
        payment_gateway=payment_gateway,
        uow=uow,
        orders=mock_orders,
        notification_service=mock_notif_service,
    )

    attempt = await use_case.process_payment(
        ProcessPaymentInput(
            checkout_id=checkout_id,
            customer_id=cust_id,
            idempotency_key="pay-confirm-notif",
            numero_tarjeta="4111 1111 1111 1111",
            titular="Diego Test",
            vencimiento="12/28",
            cvv="123",
        )
    )

    assert attempt.result == "APPROVED"
    mock_orders.actualizar_estado.assert_called_once_with(order_id, "PAID_NOTIFIED")
    mock_notif_service.enqueue_confirmation.assert_called_once_with(order_id)
    assert getattr(attempt, "confirmation", None) == {
        "correoEnmascarado": "m***a@ejemplo.com",
        "mensaje": "Te enviaremos la confirmación a m***a@…",
    }


@pytest.mark.asyncio
async def test_payment_approved_notification_failure_does_not_block_purchase(
    mock_checkouts, mock_attempts, payment_gateway
):
    checkout_id = uuid.uuid4()
    cust_id = uuid.uuid4()
    order_id = "PED-2026-99999"

    row = Checkout(
        id=checkout_id,
        cart_id=uuid.uuid4(),
        customer_id=cust_id,
        status="PENDING_PAYMENT",
        summary={"orderId": order_id},
        total=150.0,
        payment_attempts=0,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        idempotency_key="k1",
    )
    mock_checkouts.obtener_por_id.return_value = row

    mock_orders = MagicMock()
    order_ref_obj = MagicMock()
    order_ref_obj.order_id = order_id
    mock_orders.obtener_por_checkout_id = AsyncMock(return_value=order_ref_obj)
    mock_orders.actualizar_estado = AsyncMock()

    mock_notif_service = MagicMock()
    mock_notif_service.enqueue_confirmation = AsyncMock(side_effect=RuntimeError("SMTP or Outbox down"))

    uow = MagicMock()
    uow.commit = AsyncMock()

    use_case = ManageCheckoutUseCase(
        checkouts=mock_checkouts,
        payment_attempts=mock_attempts,
        payment_gateway=payment_gateway,
        uow=uow,
        orders=mock_orders,
        notification_service=mock_notif_service,
    )

    attempt = await use_case.process_payment(
        ProcessPaymentInput(
            checkout_id=checkout_id,
            customer_id=cust_id,
            idempotency_key="pay-fail-notif",
            numero_tarjeta="4111 1111 1111 1111",
            titular="Diego Test",
            vencimiento="12/28",
            cvv="123",
        )
    )

    assert attempt.result == "APPROVED"
    mock_orders.actualizar_estado.assert_called_once_with(order_id, "PAID_NOTIFIED")
    mock_notif_service.enqueue_confirmation.assert_called_once_with(order_id)


