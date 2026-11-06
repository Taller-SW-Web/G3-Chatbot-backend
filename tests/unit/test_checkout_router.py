"""Unit tests for checkout HTTP router (SPEC-14, contratos-integracion §2.5)."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from src.adapters.inbound.http.checkout_router import create_checkout_router
from src.adapters.inbound.http.dependencies.caller_identity import get_caller_identity
from src.adapters.inbound.http.problem import register_problem_handlers
from src.adapters.outbound.persistence.models.checkout import Checkout
from src.adapters.outbound.persistence.models.payment_attempt import PaymentAttempt
from src.application.use_cases.manage_checkout_use_case import CheckoutError
from src.domain.value_objects.caller_identity import CallerIdentity


@pytest.fixture
def customer_id():
    return uuid.uuid4()


@pytest.fixture
def mock_service():
    service = MagicMock()
    service.create_checkout = AsyncMock()
    service.get_checkout = AsyncMock()
    service.process_payment = AsyncMock()
    return service


@pytest.fixture
def test_app(mock_service, customer_id):
    app = FastAPI()
    register_problem_handlers(app)

    async def _caller_override():
        return CallerIdentity(customer_id=customer_id)

    app.dependency_overrides[get_caller_identity] = _caller_override

    router = create_checkout_router(lambda: mock_service)
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.mark.asyncio
async def test_post_checkout_requires_idempotency_key(test_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_app), base_url="http://test") as client:
        res = await client.post(
            "/api/v1/checkout",
            json={"total": 120.0},
        )
        assert res.status_code == 400
        data = res.json()
        assert data["code"] == "VALIDACION"


@pytest.mark.asyncio
async def test_post_checkout_creates_session_201(test_app, mock_service, customer_id):
    chk_id = uuid.uuid4()
    cart_id = uuid.uuid4()
    mock_service.create_checkout.return_value = Checkout(
        id=chk_id,
        cart_id=cart_id,
        customer_id=customer_id,
        status="PENDING_PAYMENT",
        summary={"total": 120.0},
        total=120.0,
        payment_attempts=0,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        idempotency_key="idemp-1",
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_app), base_url="http://test") as client:
        res = await client.post(
            "/api/v1/checkout",
            headers={"Idempotency-Key": "idemp-1"},
            json={
                "lineas": [{"sku": "FORGED", "precio": 1}],
                "subtotal": 1.0,
                "costoEnvio": 0.0,
                "descuento": 999.0,
                "total": 1.0,
            },
        )
        assert res.status_code == 201
        data = res.json()
        assert data["id"] == str(chk_id)
        assert data["status"] == "PENDING_PAYMENT"
        assert data["total"] == 120.0
        assert data["intentosRestantes"] == 3
        submitted = mock_service.create_checkout.call_args.args[0]
        assert not any(hasattr(submitted, field) for field in (
            "lineas", "subtotal", "costo_envio", "descuento", "total"
        ))


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", [
    ("DATO_INVALIDO", 400),
    ("RECURSO_NO_ENCONTRADO", 404),
    ("CARRITO_VACIO", 409),
])
async def test_post_checkout_contract_errors_use_problem_json(test_app, mock_service, code, status):
    mock_service.create_checkout.side_effect = CheckoutError(code, "Invalid checkout")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_app), base_url="http://test") as client:
        res = await client.post(
            "/api/v1/checkout",
            headers={"Idempotency-Key": "idemp-error"},
            json={"contacto": "invalid" if code == "DATO_INVALIDO" else {}},
        )
    assert res.status_code == status
    assert res.headers["content-type"] == "application/problem+json"
    assert res.json()["code"] == code


@pytest.mark.asyncio
async def test_get_checkout_success(test_app, mock_service, customer_id):
    chk_id = uuid.uuid4()
    mock_service.get_checkout.return_value = Checkout(
        id=chk_id,
        cart_id=uuid.uuid4(),
        customer_id=customer_id,
        status="PENDING_PAYMENT",
        summary={"total": 85.0},
        total=85.0,
        payment_attempts=1,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=12),
        idempotency_key="idemp-2",
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_app), base_url="http://test") as client:
        res = await client.get(f"/api/v1/checkout/{chk_id}")
        assert res.status_code == 200
        data = res.json()
        assert data["id"] == str(chk_id)
        assert data["intentosRestantes"] == 2


@pytest.mark.asyncio
async def test_process_payment_approved_200(test_app, mock_service, customer_id):
    chk_id = uuid.uuid4()
    attempt = PaymentAttempt(
        id=uuid.uuid4(),
        checkout_id=chk_id,
        idempotency_key="pay-key-1",
        result="APPROVED",
        brand="VISA",
        last4="1111",
        amount=150.0,
        transaction_id="SIM-12345",
    )
    attempt.confirmation = {
        "correoEnmascarado": "m***a@ejemplo.com",
        "mensaje": "Te enviaremos la confirmación a m***a@ejemplo.com",
    }
    mock_service.process_payment.return_value = attempt

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_app), base_url="http://test") as client:
        res = await client.post(
            f"/api/v1/checkout/{chk_id}/pago",
            headers={"Idempotency-Key": "pay-key-1"},
            json={
                "numeroTarjeta": "4111 1111 1111 1111",
                "titular": "Diego Dev",
                "vencimiento": "12/28",
                "cvv": "123",
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["resultado"] == "APROBADO"
        assert data["marca"] == "VISA"
        assert data["ultimos4"] == "1111"
        assert data["idTransaccion"] == "SIM-12345"
        assert data["correoEnmascarado"] == "m***a@ejemplo.com"
        assert data["mensaje"] == "Te enviaremos la confirmación a m***a@ejemplo.com"


@pytest.mark.asyncio
async def test_process_payment_rejected_402_with_motivo_and_intentos(test_app, mock_service):
    chk_id = uuid.uuid4()
    mock_service.process_payment.side_effect = CheckoutError(
        "PAGO_RECHAZADO",
        "Tu tarjeta fue rechazada por fondos insuficientes.",
        {"motivo": "FONDOS_INSUFICIENTES", "intentosRestantes": 2},
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_app), base_url="http://test") as client:
        res = await client.post(
            f"/api/v1/checkout/{chk_id}/pago",
            headers={"Idempotency-Key": "pay-key-2"},
            json={
                "numeroTarjeta": "4000 0000 0000 0002",
                "titular": "Diego Fondos",
                "vencimiento": "12/28",
                "cvv": "123",
            },
        )
        assert res.status_code == 402
        data = res.json()
        assert data["code"] == "PAGO_RECHAZADO"
        assert data["motivo"] == "FONDOS_INSUFICIENTES"
        assert data["intentosRestantes"] == 2


@pytest.mark.asyncio
async def test_process_payment_expired_410(test_app, mock_service):
    chk_id = uuid.uuid4()
    mock_service.process_payment.side_effect = CheckoutError(
        "CHECKOUT_EXPIRADO",
        "El checkout venció hace más de 15 minutos",
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_app), base_url="http://test") as client:
        res = await client.post(
            f"/api/v1/checkout/{chk_id}/pago",
            headers={"Idempotency-Key": "pay-key-3"},
            json={
                "numeroTarjeta": "4111 1111 1111 1111",
                "titular": "Diego Dev",
                "vencimiento": "12/28",
                "cvv": "123",
            },
        )
        assert res.status_code == 410
        data = res.json()
        assert data["code"] == "CHECKOUT_EXPIRADO"
