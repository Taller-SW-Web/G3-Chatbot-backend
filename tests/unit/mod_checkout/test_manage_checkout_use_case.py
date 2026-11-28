"""Unit tests for checkout and payment processing use case (SPEC-14)."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.modules.mod_checkout.adapters.payment_simulator_adapter import PaymentSimulatorAdapter
from src.modules.mod_checkout.application.manage_checkout_use_case import (
    CheckoutError,
    CreateCheckoutInput,
    ManageCheckoutUseCase,
    ProcessPaymentInput,
)
from src.modules.mod_checkout.domain.checkout import CheckoutStatus
from src.modules.mod_checkout.domain.payment_attempt import PaymentResult
from src.modules.mod_checkout.ports.payment_gateway_port import PaymentProcessResult
from src.shared.domain.module_errors import ModuleUnavailableError
from src.shared.infrastructure.db.models.checkout import Checkout
from src.shared.infrastructure.db.models.payment_attempt import PaymentAttempt
from tests.fakes.fake_commerce import FakeDispatch

VALID_ADDRESS = {"direccionExacta": "Av. Larco 1234", "distrito": "Miraflores"}

VALID_CONTACT = {
    "nombreCompleto": "Juan Pérez Rodríguez",
    "tipoDocumento": "DNI",
    "numeroDocumento": "72458912",
    "telefono": "+51999888777",
    "email": "juan@example.com",
}


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
    cart_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    carts = MagicMock()
    carts.obtener_por_id = AsyncMock(return_value=SimpleNamespace(
        id=cart_id,
        customer_id=customer_id,
        totals_snapshot={
            "subtotal": "100.00",
            "descuentos": [],
            "total": "100.00",
            "promociones_ok": True,
        },
    ))
    carts.cambiar_estado = AsyncMock()
    items = MagicMock()
    items.listar_por_cart = AsyncMock(return_value=[SimpleNamespace(sku="SKU-1", name="Polo", quantity=2, unit_price_ref=50, image_url=None)])
    return ManageCheckoutUseCase(
        checkouts=mock_checkouts,
        payment_attempts=mock_attempts,
        payment_gateway=payment_gateway,
        uow=uow,
        carts=carts,
        cart_items=items,
        dispatch=FakeDispatch(),
    )


@pytest.mark.asyncio
async def test_create_checkout_requires_idempotency_key(use_case):
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.create_checkout(
            CreateCheckoutInput(
                customer_id=uuid.uuid4(),
                idempotency_key="",
            )
        )
    assert exc_info.value.code == "VALIDACION"


@pytest.mark.asyncio
async def test_create_checkout_success_with_15_min_expiry(use_case, mock_checkouts):
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    cust_id = cart.customer_id
    cart_id = cart.id
    before = datetime.now(timezone.utc)

    checkout = await use_case.create_checkout(
        CreateCheckoutInput(
            customer_id=cust_id,
            idempotency_key="idemp-chk-1",
            cart_id=cart_id,
            contacto=VALID_CONTACT,
            direccion=VALID_ADDRESS,
        )
    )

    assert checkout.customer_id == cust_id
    assert checkout.cart_id == cart_id
    assert checkout.status == CheckoutStatus.PENDING_PAYMENT.value
    assert checkout.total == 112.50
    assert checkout.payment_attempts == 0
    assert checkout.expires_at >= before + timedelta(minutes=14, seconds=55)


@pytest.mark.asyncio
async def test_create_checkout_idempotent_returns_existing(use_case, mock_checkouts):
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    cart_id = cart.id
    existing = Checkout(
        id=uuid.uuid4(),
        cart_id=cart_id,
        customer_id=cart.customer_id,
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
            contacto=VALID_CONTACT,
        )
    )
    assert res == existing


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("nombreCompleto", ""),
    ("tipoDocumento", "OTHER"),
    ("numeroDocumento", "1234567"),
    ("numeroDocumento", "30458912345"),
    ("telefono", 123),
    ("email", None),
])
async def test_create_checkout_rejects_invalid_contact(use_case, mock_checkouts, field, value):
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    contacto = {**VALID_CONTACT, field: value}
    if field == "numeroDocumento" and value == "30458912345":
        contacto["tipoDocumento"] = "RUC"
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.create_checkout(CreateCheckoutInput(
            customer_id=cart.customer_id,
            idempotency_key="invalid-contact",
            cart_id=cart.id,
            contacto=contacto,
        ))
    assert exc_info.value.code == "DATO_INVALIDO"
    mock_checkouts.guardar.assert_not_called()


@pytest.mark.asyncio
async def test_create_checkout_accepts_contact_with_only_required_fields(use_case, mock_checkouts):
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    contacto = {
        "nombreCompleto": "Juan Pérez Rodríguez",
        "tipoDocumento": "DNI",
        "numeroDocumento": "72458912",
    }
    checkout = await use_case.create_checkout(CreateCheckoutInput(
        customer_id=cart.customer_id,
        idempotency_key="minimal-contact",
        cart_id=cart.id,
        contacto=contacto,
        direccion=VALID_ADDRESS,
    ))
    assert checkout.summary["contacto"] == contacto
    assert checkout.total == 112.50
    mock_checkouts.guardar.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("contacto", [None, "invalid", []])
async def test_create_checkout_rejects_missing_or_malformed_contact(
    use_case, mock_checkouts, contacto
):
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.create_checkout(CreateCheckoutInput(
            customer_id=cart.customer_id,
            idempotency_key="bad-contact-shape",
            cart_id=cart.id,
            contacto=contacto,
        ))
    assert exc_info.value.code == "DATO_INVALIDO"
    mock_checkouts.guardar.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [False, True])
async def test_create_checkout_rejects_missing_or_foreign_cart_before_insert(
    use_case, mock_checkouts, foreign
):
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    use_case.carts.obtener_por_id.return_value = (
        SimpleNamespace(id=cart.id, customer_id=uuid.uuid4()) if foreign else None
    )
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.create_checkout(CreateCheckoutInput(
            customer_id=cart.customer_id,
            idempotency_key="unknown-cart",
            cart_id=cart.id,
            contacto=VALID_CONTACT,
        ))
    assert exc_info.value.code == "RECURSO_NO_ENCONTRADO"
    mock_checkouts.guardar.assert_not_called()


@pytest.mark.asyncio
async def test_create_checkout_rejects_empty_cart_without_client_fallback(use_case, mock_checkouts):
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    use_case.cart_items.listar_por_cart.return_value = []
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.create_checkout(CreateCheckoutInput(
            customer_id=cart.customer_id,
            idempotency_key="empty-cart",
            cart_id=cart.id,
            contacto=VALID_CONTACT,
        ))
    assert exc_info.value.code == "CARRITO_VACIO"
    mock_checkouts.guardar.assert_not_called()


@pytest.mark.asyncio
async def test_create_checkout_uses_only_cart_discount_snapshot(use_case):
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    cart.totals_snapshot = {
        "subtotal": "100.00",
        "descuentos": [{"name": "PROMO", "amount": "10.00"}],
        "total": "90.00",
        "promociones_ok": True,
    }
    checkout = await use_case.create_checkout(CreateCheckoutInput(
        customer_id=cart.customer_id,
        idempotency_key="server-discount",
        cart_id=cart.id,
        contacto=VALID_CONTACT,
        direccion=VALID_ADDRESS,
    ))
    assert checkout.summary["descuento"] == 10.0
    assert checkout.total == 102.50


async def _create(use_case, key, direccion=VALID_ADDRESS):
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    return cart, await use_case.create_checkout(CreateCheckoutInput(
        customer_id=cart.customer_id,
        idempotency_key=key,
        cart_id=cart.id,
        contacto=VALID_CONTACT,
        direccion=direccion,
    ))


@pytest.mark.asyncio
async def test_create_checkout_uses_quoted_shipping_cost(use_case):
    use_case.dispatch.response = {
        "coberturaDisponible": True,
        "costoEnvio": 18.9,
        "moneda": "PEN",
        "plazoEstimadoDiasHabiles": 3,
    }
    _, checkout = await _create(use_case, "quoted-cost")
    assert checkout.summary["costoEnvio"] == 18.9
    assert checkout.summary["plazoEstimadoDias"] == 3
    assert checkout.summary["moneda"] == "PEN"
    assert checkout.total == 118.9
    call = use_case.dispatch.calls[0]
    assert call["lineas"] == [{"sku": "SKU-1", "cantidad": 2}]
    # El contrato de cotizacion de Despacho solo documenta distrito y direccion.
    assert call["destino"]["direccion"] == "Av. Larco 1234"
    assert call["destino"]["distrito"] == "Miraflores"
    assert "destinatario" not in call["destino"]


@pytest.mark.asyncio
async def test_create_checkout_without_coverage_raises_sin_cobertura(use_case, mock_checkouts):
    use_case.dispatch.response = {"coberturaDisponible": False}
    with pytest.raises(CheckoutError) as exc_info:
        await _create(use_case, "no-coverage")
    assert exc_info.value.code == "SIN_COBERTURA"
    assert exc_info.value.extra == {"distrito": "Miraflores"}
    mock_checkouts.guardar.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"coberturaDisponible": True, "moneda": "PEN", "plazoEstimadoDiasHabiles": 2},
    {"coberturaDisponible": True, "costoEnvio": 10, "moneda": "PEN"},
])
async def test_create_checkout_quote_missing_cost_or_lead_time_is_503(
    use_case, mock_checkouts, response
):
    use_case.dispatch.response = response
    with pytest.raises(CheckoutError) as exc_info:
        await _create(use_case, "bad-quote")
    assert exc_info.value.code == "MODULO_NO_DISPONIBLE"
    assert exc_info.value.extra == {"modulo": "dispatch"}
    mock_checkouts.guardar.assert_not_called()


@pytest.mark.asyncio
async def test_create_checkout_dispatch_failure_is_503(use_case, mock_checkouts):
    use_case.dispatch.error = ModuleUnavailableError("dispatch", "timeout")
    with pytest.raises(CheckoutError) as exc_info:
        await _create(use_case, "dispatch-down")
    assert exc_info.value.code == "MODULO_NO_DISPONIBLE"
    mock_checkouts.guardar.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("direccion", [None, {"distrito": "Miraflores"}, {"direccionExacta": "Av. Larco 1234"}])
async def test_create_checkout_incomplete_address_is_validacion(use_case, mock_checkouts, direccion):
    with pytest.raises(CheckoutError) as exc_info:
        await _create(use_case, "bad-address", direccion=direccion)
    assert exc_info.value.code == "VALIDACION"
    assert exc_info.value.extra["errores"]
    assert use_case.dispatch.calls == []
    mock_checkouts.guardar.assert_not_called()


@pytest.mark.asyncio
async def test_create_checkout_stale_snapshot_raises_409_with_server_summary(use_case, mock_checkouts):
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    cart.totals_snapshot = {
        "subtotal": "80.00",
        "descuentos": [{"name": "PROMO", "amount": "10.00"}],
        "total": "70.00",
        "promociones_ok": True,
    }
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.create_checkout(CreateCheckoutInput(
            customer_id=cart.customer_id,
            idempotency_key="stale",
            cart_id=cart.id,
            contacto=VALID_CONTACT,
            direccion=VALID_ADDRESS,
        ))
    assert exc_info.value.code == "CARRITO_DESACTUALIZADO"
    assert exc_info.value.extra["motivo"] == "SUBTOTAL_CAMBIADO"
    assert exc_info.value.extra["summary"] == {
        "lineas": [{"sku": "SKU-1", "cantidad": 2, "precioUnitario": 50.0}],
        "subtotal": 100.0,
        "descuento": 0.0,
        "costoEnvio": 12.5,
        "total": 112.5,
    }
    mock_checkouts.guardar.assert_not_called()


@pytest.mark.asyncio
async def test_create_checkout_without_cart_evaluation_is_409(use_case, mock_checkouts):
    """No snapshot means nobody proved the discount: never guess a total."""
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    cart.totals_snapshot = None
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.create_checkout(CreateCheckoutInput(
            customer_id=cart.customer_id,
            idempotency_key="no-evaluation",
            cart_id=cart.id,
            contacto=VALID_CONTACT,
            direccion=VALID_ADDRESS,
        ))
    assert exc_info.value.code == "CARRITO_DESACTUALIZADO"
    assert exc_info.value.extra["motivo"] == "SIN_EVALUACION"
    mock_checkouts.guardar.assert_not_called()


@pytest.mark.asyncio
async def test_create_checkout_with_unavailable_promotions_is_409(use_case, mock_checkouts):
    """Regression: PED-2026-00088 showed S/ 49.10 off in the cart but paid full price."""
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    cart.totals_snapshot = {
        "subtotal": "100.00",
        "descuentos": [],
        "total": "100.00",
        "promociones_ok": False,
        "promotion_reason": "SERVICIO_NO_DISPONIBLE",
    }
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.create_checkout(CreateCheckoutInput(
            customer_id=cart.customer_id,
            idempotency_key="promotions-down",
            cart_id=cart.id,
            contacto=VALID_CONTACT,
            direccion=VALID_ADDRESS,
        ))
    assert exc_info.value.code == "CARRITO_DESACTUALIZADO"
    assert exc_info.value.extra["motivo"] == "PROMOCIONES_NO_DISPONIBLES"
    mock_checkouts.guardar.assert_not_called()


@pytest.mark.asyncio
async def test_create_checkout_with_legacy_snapshot_without_flag_is_409(
    use_case, mock_checkouts
):
    """Pre-deploy snapshots cannot prove promotion evaluation; re-view and retry."""
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    cart.totals_snapshot = {
        "subtotal": "100.00",
        "descuentos": [],
        "total": "100.00",
    }
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.create_checkout(CreateCheckoutInput(
            customer_id=cart.customer_id,
            idempotency_key="legacy-snapshot",
            cart_id=cart.id,
            contacto=VALID_CONTACT,
            direccion=VALID_ADDRESS,
        ))
    assert exc_info.value.extra["motivo"] == "PROMOCIONES_NO_DISPONIBLES"
    mock_checkouts.guardar.assert_not_called()


@pytest.mark.asyncio
async def test_create_checkout_with_inconsistent_snapshot_total_is_409(
    use_case, mock_checkouts
):
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    cart.totals_snapshot = {
        "subtotal": "100.00",
        "descuentos": [{"name": "PROMO", "amount": "10.00"}],
        "total": "100.00",
        "promociones_ok": True,
    }
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.create_checkout(CreateCheckoutInput(
            customer_id=cart.customer_id,
            idempotency_key="broken-total",
            cart_id=cart.id,
            contacto=VALID_CONTACT,
            direccion=VALID_ADDRESS,
        ))
    assert exc_info.value.extra["motivo"] == "TOTAL_INCONSISTENTE"
    mock_checkouts.guardar.assert_not_called()


@pytest.mark.asyncio
async def test_checkout_recalculates_stale_snapshot_and_next_attempt_succeeds(
    use_case, mock_checkouts
):
    """The 409 persists a fresh snapshot, so the UI retry does not loop."""
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    cart.totals_snapshot = {
        "subtotal": "100.00",
        "descuentos": [],
        "total": "100.00",
        "promociones_ok": False,
    }

    async def cart_totals(_cart):
        cart.totals_snapshot = {
            "subtotal": "100.00",
            "descuentos": [{"name": "PROMO", "amount": "10.00"}],
            "total": "90.00",
            "promociones_ok": True,
        }
        return {
            "lineas": [
                {"sku": "SKU-1", "cantidad": 2, "precio": 50.0, "disponible": True}
            ],
            "subtotal": 100.0,
            "descuentos": [{"nombre": "PROMO", "monto": 10.0}],
            "total": 90.0,
            "promociones_ok": True,
        }

    use_case.cart_totals = cart_totals
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.create_checkout(CreateCheckoutInput(
            customer_id=cart.customer_id,
            idempotency_key="refresh-1",
            cart_id=cart.id,
            contacto=VALID_CONTACT,
            direccion=VALID_ADDRESS,
        ))
    assert exc_info.value.extra["motivo"] == "PROMOCIONES_NO_DISPONIBLES"
    assert exc_info.value.extra["summary"]["descuento"] == 10.0
    assert exc_info.value.extra["summary"]["total"] == 102.5
    mock_checkouts.guardar.assert_not_called()

    checkout = await use_case.create_checkout(CreateCheckoutInput(
        customer_id=cart.customer_id,
        idempotency_key="refresh-2",
        cart_id=cart.id,
        contacto=VALID_CONTACT,
        direccion=VALID_ADDRESS,
    ))
    assert checkout.total == 102.5
    assert checkout.summary["descuento"] == 10.0


@pytest.mark.asyncio
async def test_checkout_recovery_failure_keeps_409_and_creates_nothing(
    use_case, mock_checkouts
):
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    cart.totals_snapshot = None

    async def cart_totals(_cart):
        raise ModuleUnavailableError("products", "timeout")

    use_case.cart_totals = cart_totals
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.create_checkout(CreateCheckoutInput(
            customer_id=cart.customer_id,
            idempotency_key="refresh-down",
            cart_id=cart.id,
            contacto=VALID_CONTACT,
            direccion=VALID_ADDRESS,
        ))
    assert exc_info.value.extra["motivo"] == "SIN_EVALUACION"
    assert exc_info.value.extra["summary"]["descuento"] == 0.0
    mock_checkouts.guardar.assert_not_called()


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
    mock_checkouts.obtener_por_id.return_value = Checkout(
        id=checkout_id,
        cart_id=uuid.uuid4(),
        customer_id=cust_id,
        status="PENDING_PAYMENT",
        summary={},
        total=150.0,
        payment_attempts=1,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        idempotency_key="checkout-key",
    )

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
async def test_payment_idempotency_retry_rejects_foreign_customer(
    use_case, mock_checkouts, mock_attempts
):
    checkout_id = uuid.uuid4()
    mock_attempts.obtener_por_idempotency_key = AsyncMock(return_value=PaymentAttempt(
        id=uuid.uuid4(),
        checkout_id=checkout_id,
        idempotency_key="foreign-retry",
        result="APPROVED",
        amount=100.0,
    ))
    mock_checkouts.obtener_por_id.return_value = Checkout(
        id=checkout_id,
        cart_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        status="PENDING_PAYMENT",
        summary={},
        total=100.0,
        payment_attempts=1,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        idempotency_key="checkout-key",
    )
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.process_payment(ProcessPaymentInput(
            checkout_id=checkout_id,
            customer_id=uuid.uuid4(),
            idempotency_key="foreign-retry",
            numero_tarjeta="4111111111111111",
            titular="Other Customer",
            vencimiento="12/28",
            cvv="123",
        ))
    assert exc_info.value.code == "RECURSO_NO_ENCONTRADO"
    mock_attempts.guardar.assert_not_called()


@pytest.mark.asyncio
async def test_create_checkout_recalculates_items_and_shipping_from_server(
    mock_checkouts, mock_attempts, payment_gateway
):
    cust_id = uuid.uuid4()
    cart_id = uuid.uuid4()

    mock_cart = MagicMock()
    mock_cart.id = cart_id
    mock_cart.customer_id = cust_id
    mock_cart.totals_snapshot = {
        "subtotal": "80.00",
        "descuentos": [],
        "total": "80.00",
        "promociones_ok": True,
    }

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
        dispatch=FakeDispatch(),
    )

    # The server calculates the subtotal and adds the quoted shipping cost.
    checkout = await use_case.create_checkout(
        CreateCheckoutInput(
            customer_id=cust_id,
            idempotency_key="chk-recalc-1",
            cart_id=None,
            contacto=VALID_CONTACT,
            direccion=VALID_ADDRESS,
        )
    )
    assert checkout.total == 92.50
    assert checkout.summary["subtotal"] == 80.0
    assert checkout.summary["costoEnvio"] == 12.50  # fake dispatch quote
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
async def test_payment_approved_keeps_the_order_pending_until_ventas_answers(
    mock_checkouts, mock_attempts, payment_gateway
):
    """Decision "Pendiente hasta G5": no PAID_NOTIFIED and no confirmation at checkout."""
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
    mock_outbox = MagicMock()
    mock_outbox.guardar = AsyncMock()

    uow = MagicMock()
    uow.commit = AsyncMock()

    use_case = ManageCheckoutUseCase(
        checkouts=mock_checkouts,
        payment_attempts=mock_attempts,
        payment_gateway=payment_gateway,
        uow=uow,
        orders=mock_orders,
        outbox=mock_outbox,
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
    mock_orders.actualizar_estado.assert_called_once_with(order_id, "PAYMENT_PENDING_NOTIFICATION")
    assert getattr(attempt, "confirmation", None) is None
    task = mock_outbox.guardar.await_args.args[0]
    assert task.type == "NOTIFY_SALES_PAYMENT"
    assert task.payload["order_id"] == order_id


@pytest.mark.asyncio
async def test_create_checkout_calls_sales_client_and_records_order_id(
    mock_checkouts, mock_attempts, payment_gateway
):
    cust_id = uuid.uuid4()
    cart_id = uuid.uuid4()
    mock_carts = MagicMock()
    mock_carts.obtener_abierto_por_cliente = AsyncMock(
        return_value=SimpleNamespace(
            id=cart_id,
            totals_snapshot={"subtotal": 100.0, "descuentos": [], "promociones_ok": True},
        )
    )
    mock_carts.cambiar_estado = AsyncMock()
    mock_cart_items = MagicMock()
    mock_cart_items.listar_por_cart = AsyncMock(
        return_value=[
            SimpleNamespace(sku="SKU-1", name="Prod 1", quantity=1, unit_price_ref=100.0, image_url=None)
        ]
    )
    mock_orders = MagicMock()
    mock_orders.guardar = AsyncMock()

    mock_sales = MagicMock()
    mock_sales.create_order = AsyncMock(return_value={"pedidoId": "PED-VENTAS-999"})

    use_case = ManageCheckoutUseCase(
        checkouts=mock_checkouts,
        payment_attempts=mock_attempts,
        payment_gateway=payment_gateway,
        carts=mock_carts,
        cart_items=mock_cart_items,
        orders=mock_orders,
        sales_client=mock_sales,
        dispatch=FakeDispatch(),
    )

    chk = await use_case.create_checkout(
        CreateCheckoutInput(
            customer_id=cust_id,
            idempotency_key="key-sales-1",
            contacto=VALID_CONTACT,
            direccion={"direccion": "Av. Prueba 123", "distrito": "Miraflores"},
        )
    )

    assert chk.summary["orderId"] == "PED-VENTAS-999"
    mock_sales.create_order.assert_awaited_once()
    snapshot = mock_sales.create_order.call_args[0][0]
    assert "cupon" not in snapshot
    saved_order_ref = mock_orders.guardar.call_args[0][0]
    assert saved_order_ref.order_id == "PED-VENTAS-999"


@pytest.mark.asyncio
async def test_create_checkout_handles_stock_insuficiente_409(
    mock_checkouts, mock_attempts, payment_gateway
):
    from src.shared.domain.module_errors import ModuleRequestError

    cust_id = uuid.uuid4()
    cart_id = uuid.uuid4()
    mock_carts = MagicMock()
    mock_carts.obtener_abierto_por_cliente = AsyncMock(
        return_value=SimpleNamespace(
            id=cart_id,
            totals_snapshot={"subtotal": 50.0, "descuentos": [], "promociones_ok": True},
        )
    )
    mock_carts.cambiar_estado = AsyncMock()
    mock_cart_items = MagicMock()
    mock_cart_items.listar_por_cart = AsyncMock(
        return_value=[
            SimpleNamespace(sku="SKU-NO-STOCK", name="Sin Stock", quantity=1, unit_price_ref=50.0, image_url=None)
        ]
    )
    mock_sales = MagicMock()
    mock_sales.create_order = AsyncMock(side_effect=ModuleRequestError("sales", 409, "STOCK_INSUFICIENTE"))

    use_case = ManageCheckoutUseCase(
        checkouts=mock_checkouts,
        payment_attempts=mock_attempts,
        payment_gateway=payment_gateway,
        carts=mock_carts,
        cart_items=mock_cart_items,
        sales_client=mock_sales,
        dispatch=FakeDispatch(),
    )

    with pytest.raises(CheckoutError) as exc:
        await use_case.create_checkout(
            CreateCheckoutInput(
                customer_id=cust_id,
                idempotency_key="key-sales-err",
                contacto=VALID_CONTACT,
                direccion={"direccion": "Av. Prueba 123", "distrito": "Miraflores"},
            )
        )
    assert exc.value.code == "STOCK_INSUFICIENTE"


@pytest.mark.asyncio
async def test_process_payment_enqueues_outbox_events(
    mock_checkouts, mock_attempts, payment_gateway
):
    cust_id = uuid.uuid4()
    checkout_id = uuid.uuid4()
    row = Checkout(
        id=checkout_id,
        cart_id=uuid.uuid4(),
        customer_id=cust_id,
        status="PENDING_PAYMENT",
        total=150.0,
        payment_attempts=0,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        idempotency_key="chk-outbox",
        summary={"orderId": "PED-OUTBOX-1"},
    )
    mock_checkouts.obtener_por_id.return_value = row

    mock_orders = MagicMock()
    order_ref = MagicMock()
    order_ref.order_id = "PED-OUTBOX-1"
    mock_orders.obtener_por_checkout_id = AsyncMock(return_value=order_ref)
    mock_orders.actualizar_estado = AsyncMock()

    mock_outbox = MagicMock()
    mock_outbox.guardar = AsyncMock()

    use_case = ManageCheckoutUseCase(
        checkouts=mock_checkouts,
        payment_attempts=mock_attempts,
        payment_gateway=payment_gateway,
        orders=mock_orders,
        outbox=mock_outbox,
    )

    # 1. Pago aprobado -> NOTIFY_SALES_PAYMENT en outbox
    attempt = await use_case.process_payment(
        ProcessPaymentInput(
            checkout_id=checkout_id,
            customer_id=cust_id,
            idempotency_key="pay-outbox-ok",
            numero_tarjeta="4111 1111 1111 1111",
            titular="Juan Perez",
            vencimiento="12/28",
            cvv="123",
        )
    )
    assert attempt.result == "APPROVED"
    mock_outbox.guardar.assert_awaited_once()
    outbox_task = mock_outbox.guardar.call_args[0][0]
    assert outbox_task.type == "NOTIFY_SALES_PAYMENT"
    assert outbox_task.payload["order_id"] == "PED-OUTBOX-1"
    assert outbox_task.payload["payment_data"]["resultado"] == "APROBADO"
    assert outbox_task.payload["payment_data"]["ultimosCuatroDigitos"] == "1111"


@pytest.mark.asyncio
async def test_outbox_payment_payload_omits_last4_when_gateway_returns_none(
    mock_checkouts, mock_attempts
):
    cust_id = uuid.uuid4()
    checkout_id = uuid.uuid4()
    row = Checkout(
        id=checkout_id,
        cart_id=uuid.uuid4(),
        customer_id=cust_id,
        status="PENDING_PAYMENT",
        total=150.0,
        payment_attempts=0,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        idempotency_key="chk-outbox-no-last4",
        summary={"orderId": "PED-OUTBOX-2"},
    )
    mock_checkouts.obtener_por_id.return_value = row

    mock_orders = MagicMock()
    order_ref = MagicMock()
    order_ref.order_id = "PED-OUTBOX-2"
    mock_orders.obtener_por_checkout_id = AsyncMock(return_value=order_ref)
    mock_orders.actualizar_estado = AsyncMock()

    mock_outbox = MagicMock()
    mock_outbox.guardar = AsyncMock()

    class GatewayWithoutLast4:
        async def process_payment(self, *, card_number, holder_name, expiry, cvv, amount):
            return PaymentProcessResult(
                result=PaymentResult.APPROVED.value,
                brand="VISA",
                last4="",
                amount=amount,
                transaction_id="SIM-NO-LAST4",
            )

    use_case = ManageCheckoutUseCase(
        checkouts=mock_checkouts,
        payment_attempts=mock_attempts,
        payment_gateway=GatewayWithoutLast4(),
        orders=mock_orders,
        outbox=mock_outbox,
    )

    await use_case.process_payment(
        ProcessPaymentInput(
            checkout_id=checkout_id,
            customer_id=cust_id,
            idempotency_key="pay-outbox-no-last4",
            numero_tarjeta="4111 1111 1111 1111",
            titular="Juan Perez",
            vencimiento="12/28",
            cvv="123",
        )
    )

    mock_outbox.guardar.assert_awaited_once()
    payment_data = mock_outbox.guardar.call_args[0][0].payload["payment_data"]
    assert "ultimosCuatroDigitos" not in payment_data





@pytest.mark.asyncio
async def test_create_checkout_maps_snapshot_validation_error_to_409_with_server_summary(
    mock_checkouts, mock_attempts, payment_gateway
):
    cust_id = uuid.uuid4()
    mock_carts = MagicMock()
    mock_carts.obtener_abierto_por_cliente = AsyncMock(
        return_value=SimpleNamespace(
            id=uuid.uuid4(),
            totals_snapshot={"subtotal": 100.0, "descuentos": [], "promociones_ok": True},
        )
    )
    mock_carts.cambiar_estado = AsyncMock()
    mock_cart_items = MagicMock()
    # A line without SKU makes SnapshotBuilder raise SnapshotValidationError.
    mock_cart_items.listar_por_cart = AsyncMock(
        return_value=[SimpleNamespace(sku=" ", name="Sin SKU", quantity=1, unit_price_ref=100.0, image_url=None)]
    )
    mock_sales = MagicMock()
    mock_sales.create_order = AsyncMock()
    use_case = ManageCheckoutUseCase(
        checkouts=mock_checkouts,
        payment_attempts=mock_attempts,
        payment_gateway=payment_gateway,
        carts=mock_carts,
        cart_items=mock_cart_items,
        sales_client=mock_sales,
        dispatch=FakeDispatch(),
    )

    with pytest.raises(CheckoutError) as exc:
        await use_case.create_checkout(
            CreateCheckoutInput(
                customer_id=cust_id,
                idempotency_key="key-snapshot-invalid",
                contacto=VALID_CONTACT,
                direccion={"direccion": "Av. Prueba 123", "distrito": "Miraflores"},
            )
        )

    assert exc.value.code == "CARRITO_DESACTUALIZADO"
    assert exc.value.extra["summary"]["subtotal"] == 100.0
    mock_sales.create_order.assert_not_awaited()
    mock_checkouts.guardar.assert_not_called()


# --- Retry with a new idempotency key while a checkout is alive ---


def _item(sku, quantity, price):
    return SimpleNamespace(sku=sku, name=sku, quantity=quantity, unit_price_ref=price, image_url=None)


async def _first_checkout(use_case, mock_checkouts, key="key-1"):
    """Creates a checkout and makes it the live one returned for the cart."""
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    checkout = await use_case.create_checkout(CreateCheckoutInput(
        customer_id=cart.customer_id,
        idempotency_key=key,
        cart_id=cart.id,
        contacto=VALID_CONTACT,
        direccion=VALID_ADDRESS,
    ))
    mock_checkouts.obtener_vivo_por_cart.return_value = checkout
    mock_checkouts.guardar.reset_mock()
    return cart, checkout


async def _retry(use_case, cart, direccion=VALID_ADDRESS, key="key-2"):
    return await use_case.create_checkout(CreateCheckoutInput(
        customer_id=cart.customer_id,
        idempotency_key=key,
        cart_id=cart.id,
        contacto=VALID_CONTACT,
        direccion=direccion,
    ))


@pytest.mark.asyncio
async def test_retry_with_new_key_and_same_data_reuses_checkout(use_case, mock_checkouts):
    cart, first = await _first_checkout(use_case, mock_checkouts)
    res = await _retry(use_case, cart)
    assert res is first
    mock_checkouts.guardar.assert_not_called()
    mock_checkouts.marcar_estado.assert_not_called()


@pytest.mark.asyncio
async def test_retry_with_changed_address_expires_old_and_creates_new(use_case, mock_checkouts):
    cart, first = await _first_checkout(use_case, mock_checkouts)
    res = await _retry(use_case, cart, direccion={**VALID_ADDRESS, "distrito": "Surco"})
    assert res is not first
    assert res.idempotency_key == "key-2"
    mock_checkouts.marcar_estado.assert_awaited_once_with(first.id, CheckoutStatus.EXPIRED.value)
    mock_checkouts.guardar.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_after_adding_item_expires_old_and_creates_new_with_new_subtotal(use_case, mock_checkouts):
    cart, first = await _first_checkout(use_case, mock_checkouts)
    use_case.cart_items.listar_por_cart.return_value = [_item("SKU-1", 2, 50), _item("SKU-2", 1, 20)]
    cart.totals_snapshot = {
        "subtotal": "120.00",
        "descuentos": [],
        "total": "120.00",
        "promociones_ok": True,
    }
    res = await _retry(use_case, cart)
    assert res is not first
    assert res.summary["subtotal"] == 120.0
    assert len(res.summary["lineas"]) == 2
    mock_checkouts.marcar_estado.assert_awaited_once_with(first.id, CheckoutStatus.EXPIRED.value)
    mock_checkouts.guardar.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_after_price_change_creates_new_checkout(use_case, mock_checkouts):
    cart, first = await _first_checkout(use_case, mock_checkouts)
    use_case.cart_items.listar_por_cart.return_value = [_item("SKU-1", 2, 55)]
    cart.totals_snapshot = {
        "subtotal": "110.00",
        "descuentos": [],
        "total": "110.00",
        "promociones_ok": True,
    }
    res = await _retry(use_case, cart)
    assert res is not first
    assert res.summary["subtotal"] == 110.0
    mock_checkouts.marcar_estado.assert_awaited_once_with(first.id, CheckoutStatus.EXPIRED.value)


@pytest.mark.asyncio
async def test_retry_after_expires_at_passed_creates_new_checkout(use_case, mock_checkouts):
    cart, first = await _first_checkout(use_case, mock_checkouts)
    first.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    res = await _retry(use_case, cart)
    assert res is not first
    mock_checkouts.marcar_estado.assert_awaited_once_with(first.id, CheckoutStatus.EXPIRED.value)
    mock_checkouts.guardar.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_with_approved_checkout_returns_it_even_if_data_changed(use_case, mock_checkouts):
    cart, first = await _first_checkout(use_case, mock_checkouts)
    first.status = CheckoutStatus.PAYMENT_APPROVED.value
    res = await _retry(use_case, cart, direccion={**VALID_ADDRESS, "distrito": "Surco"})
    assert res is first
    mock_checkouts.guardar.assert_not_called()
    mock_checkouts.marcar_estado.assert_not_called()


@pytest.mark.asyncio
async def test_subtotal_sums_prices_with_decimal_precision(use_case):
    use_case.cart_items.listar_por_cart.return_value = [_item("SKU-1", 1, 329.9), _item("SKU-2", 1, 79.9)]
    cart = await use_case.carts.obtener_por_id(uuid.uuid4())
    cart.totals_snapshot = {
        "subtotal": "409.80",
        "descuentos": [],
        "total": "409.80",
        "promociones_ok": True,
    }
    _, checkout = await _create(use_case, "decimal-key")
    assert checkout.summary["subtotal"] == 409.8
    assert checkout.summary["lineas"][0]["precio"] == 329.9


@pytest.mark.asyncio
@pytest.mark.parametrize("result,reason,code", [
    ("REJECTED", "INSUFFICIENT_FUNDS", "PAGO_RECHAZADO"),
    ("ERROR", "PROCESSING_ERROR", "ERROR_PROCESAMIENTO"),
])
async def test_payment_replay_of_non_approved_attempt_reraises_original_outcome(
    use_case, mock_checkouts, mock_attempts, result, reason, code
):
    checkout_id = uuid.uuid4()
    cust_id = uuid.uuid4()
    mock_checkouts.obtener_por_id.return_value = Checkout(
        id=checkout_id, cart_id=uuid.uuid4(), customer_id=cust_id, status="PENDING_PAYMENT",
        summary={}, total=100.0, payment_attempts=1,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10), idempotency_key="k1",
    )
    mock_attempts.obtener_por_idempotency_key = AsyncMock(
        return_value=PaymentAttempt(
            id=uuid.uuid4(), checkout_id=checkout_id, idempotency_key="pay-key-1",
            result=result, reason=reason, brand="VISA", last4="1111", amount=100.0,
        )
    )
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.process_payment(
            ProcessPaymentInput(
                checkout_id=checkout_id, customer_id=cust_id, idempotency_key="pay-key-1",
                numero_tarjeta="4000000000000002", titular="Diego Test", vencimiento="12/28", cvv="123",
            )
        )
    assert exc_info.value.code == code
    assert exc_info.value.extra["intentosRestantes"] == 2


def _pending_checkout(checkout_id, cust_id, attempts=0):
    return Checkout(
        id=checkout_id, cart_id=uuid.uuid4(), customer_id=cust_id, status="PENDING_PAYMENT",
        summary={}, total=150.0, payment_attempts=attempts,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10), idempotency_key="k1",
    )


def _payment_input(checkout_id, cust_id, card="4111 1111 1111 1111", key="pay-key-lock"):
    return ProcessPaymentInput(
        checkout_id=checkout_id, customer_id=cust_id, idempotency_key=key,
        numero_tarjeta=card, titular="Diego Valido", vencimiento="10/29", cvv="456",
    )


@pytest.mark.asyncio
async def test_payment_locks_the_checkout_row(use_case, mock_checkouts):
    checkout_id, cust_id = uuid.uuid4(), uuid.uuid4()
    mock_checkouts.obtener_por_id.return_value = _pending_checkout(checkout_id, cust_id)
    await use_case.process_payment(_payment_input(checkout_id, cust_id))
    mock_checkouts.obtener_por_id.assert_any_call(checkout_id, for_update=True)


@pytest.mark.asyncio
async def test_concurrent_approved_payment_maps_to_conflict(use_case, mock_checkouts, mock_attempts):
    from src.modules.mod_checkout.domain.payment_attempt import PaymentConflictError

    checkout_id, cust_id = uuid.uuid4(), uuid.uuid4()
    mock_checkouts.obtener_por_id.return_value = _pending_checkout(checkout_id, cust_id)
    mock_attempts.guardar = AsyncMock(side_effect=PaymentConflictError("dup"))
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.process_payment(_payment_input(checkout_id, cust_id))
    assert exc_info.value.code == "CONFLICTO"


@pytest.mark.asyncio
async def test_attempt_counter_at_cap_reports_exhausted_instead_of_overflowing(use_case, mock_checkouts):
    checkout_id, cust_id = uuid.uuid4(), uuid.uuid4()
    mock_checkouts.obtener_por_id.return_value = _pending_checkout(checkout_id, cust_id, attempts=2)
    mock_checkouts.incrementar_intentos = AsyncMock(return_value=None)
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.process_payment(_payment_input(checkout_id, cust_id, card="4000 0000 0000 0002"))
    assert exc_info.value.code == "PAGO_RECHAZADO"
    assert exc_info.value.extra["intentosRestantes"] == 0


@pytest.mark.asyncio
async def test_same_key_won_under_the_lock_is_replayed_not_reprocessed(use_case, mock_checkouts, mock_attempts):
    checkout_id, cust_id = uuid.uuid4(), uuid.uuid4()
    mock_checkouts.obtener_por_id.return_value = _pending_checkout(checkout_id, cust_id)
    winner = PaymentAttempt(
        id=uuid.uuid4(), checkout_id=checkout_id, idempotency_key="pay-key-lock", result="APPROVED",
        brand="VISA", last4="1111", amount=150.0,
    )
    mock_attempts.obtener_por_idempotency_key = AsyncMock(side_effect=[None, winner])
    attempt = await use_case.process_payment(_payment_input(checkout_id, cust_id))
    assert attempt is winner
    mock_checkouts.marcar_estado.assert_not_called()


def _expired_checkout(order_id="ord-1"):
    return Checkout(
        id=uuid.uuid4(), cart_id=uuid.uuid4(), customer_id=uuid.uuid4(), status="PENDING_PAYMENT",
        summary={"orderId": order_id}, total=100.0, payment_attempts=0,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1), idempotency_key="k-exp",
    )


def _wire_expiry(use_case, row):
    use_case.orders = MagicMock()
    use_case.orders.obtener_por_checkout_id = AsyncMock(return_value=SimpleNamespace(order_id=row.summary["orderId"]))
    use_case.orders.actualizar_estado = AsyncMock()
    use_case.outbox = MagicMock()
    use_case.outbox.guardar = AsyncMock()


def _assert_fully_expired(use_case, mock_checkouts, row):
    mock_checkouts.marcar_estado.assert_called_with(row.id, CheckoutStatus.EXPIRED.value)
    use_case.carts.cambiar_estado.assert_called_with(row.cart_id, "ACTIVE")
    use_case.orders.actualizar_estado.assert_called_with("ord-1", "CANCELLED")
    task = use_case.outbox.guardar.call_args.args[0]
    assert task.type == "REQUEST_CANCELLATION"
    assert task.payload["order_id"] == "ord-1"


@pytest.mark.asyncio
async def test_get_checkout_lazy_expiry_enqueues_the_ventas_cancellation(use_case, mock_checkouts):
    row = _expired_checkout()
    _wire_expiry(use_case, row)
    mock_checkouts.obtener_por_id.return_value = row
    result = await use_case.get_checkout(row.id, row.customer_id)
    assert result.status == "EXPIRED"
    _assert_fully_expired(use_case, mock_checkouts, row)


@pytest.mark.asyncio
async def test_sweeper_expires_due_checkouts_with_all_effects(use_case, mock_checkouts):
    row = _expired_checkout()
    _wire_expiry(use_case, row)
    mock_checkouts.listar_vencidos_para_expirar = AsyncMock(return_value=[row])
    assert await use_case.expire_due_checkouts() == 1
    _assert_fully_expired(use_case, mock_checkouts, row)
    use_case.uow.commit.assert_awaited()


@pytest.mark.asyncio
async def test_sweeper_does_nothing_when_nothing_is_due(use_case, mock_checkouts):
    mock_checkouts.listar_vencidos_para_expirar = AsyncMock(return_value=[])
    assert await use_case.expire_due_checkouts() == 0
    use_case.uow.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_expire_checkout_is_idempotent_once_expired(use_case, mock_checkouts):
    row = _expired_checkout()
    row.status = "EXPIRED"
    _wire_expiry(use_case, row)
    mock_checkouts.obtener_por_id.return_value = row
    assert await use_case.expire_checkout(row.id) is False
    use_case.outbox.guardar.assert_not_awaited()


@pytest.mark.asyncio
async def test_payment_on_overdue_checkout_expires_it_through_the_same_routine(use_case, mock_checkouts):
    row = _expired_checkout()
    _wire_expiry(use_case, row)
    mock_checkouts.obtener_por_id.return_value = row
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.process_payment(_payment_input(row.id, row.customer_id))
    assert exc_info.value.code == "CHECKOUT_EXPIRADO"
    _assert_fully_expired(use_case, mock_checkouts, row)
