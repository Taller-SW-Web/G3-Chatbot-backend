"""Issue 4.3: integration coverage for SPEC-16 (confirmation e-mail) and SPEC-17.

Real PostgreSQL 18 for the notification/outbox/order_ref tables and the real
`NotificationService`, templates and outbox adapter. HTTP evidence for the
order query goes through the real router and `OrdersService` with the Ventas
fake used by `SALES_MODE=fake`.
"""
import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.modules.mod_cart.adapters.cart_postgres_adapter import CartPostgresAdapter
from src.modules.mod_orders.adapters.fake_email_sender import FakeEmailSender
from src.modules.mod_orders.adapters.http.orders_router import create_orders_router
from src.modules.mod_orders.adapters.jinja_email_renderer import JinjaEmailRenderer
from src.modules.mod_orders.adapters.notification_postgres_adapter import (
    NotificationPostgresAdapter,
)
from src.modules.mod_orders.adapters.order_confirmation_source import (
    OrderConfirmationPostgresAdapter,
)
from src.modules.mod_orders.application.notification_service import (
    NotificationService,
    SendOutcome,
)
from src.modules.mod_orders.application.orders_service import OrdersService
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.infrastructure.db.models import Checkout, OrderRef
from src.shared.infrastructure.fakes.fake_ventas import FakeSalesPort
from src.shared.infrastructure.http.dependencies.caller_identity import get_caller_identity
from src.shared.infrastructure.http.problem import register_problem_handlers
from src.shared.infrastructure.persistence.outbox_postgres_adapter import OutboxPostgresAdapter
from src.shared.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CUSTOMER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
OTHER_CUSTOMER = uuid.UUID("99999999-8888-7777-6666-555555555555")
ORDER_ID = "PED-2026-00991"
SUMMARY = {
    "lineas": [
        {"sku": "ZAP-RUN-42-NEG", "nombre": "Zapatilla Running X", "talla": "42",
         "color": "Negro", "cantidad": 2, "precioUnitario": 120.00},
    ],
    "subtotal": 240.00,
    "descuento": 0,
    "costoEnvio": 12.50,
    "total": 252.50,
    "envio": {"plazoEstimadoDias": 2},
    "direccion": {"destinatario": "María Pérez", "distrito": "Miraflores",
                  "direccionExacta": "Av. Larco 1234"},
    "contacto": {"nombreCompleto": "María Pérez", "email": "maria@ejemplo.com"},
}


async def _paid_order(session, *, order_id: str = ORDER_ID,
                     customer_id: uuid.UUID = CUSTOMER_ID) -> None:
    cart = await CartPostgresAdapter(session).crear_para_cliente(customer_id)
    checkout = Checkout(
        cart_id=cart.id,
        customer_id=customer_id,
        status="PAYMENT_APPROVED",
        summary=SUMMARY,
        total=252.50,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        idempotency_key=f"checkout-{uuid.uuid4().hex}",
    )
    session.add(checkout)
    await session.flush()
    session.add(OrderRef(
        order_id=order_id, customer_id=customer_id, checkout_id=checkout.id,
        local_status="PAID_NOTIFIED", total=252.50,
    ))
    await session.flush()


def _service(session, sender: FakeEmailSender, *, send_inline: bool = False) -> NotificationService:
    return NotificationService(
        notifications=NotificationPostgresAdapter(session),
        outbox=OutboxPostgresAdapter(session),
        sources=OrderConfirmationPostgresAdapter(session),
        sender=sender,
        templates=JinjaEmailRenderer(),
        uow=SqlAlchemyUnitOfWork(session),
        frontend_base_url="http://localhost:3000",
        send_inline=send_inline,
    )


async def _outbox_rows(session) -> list[Any]:
    from sqlalchemy import select

    from src.shared.infrastructure.db.models import Outbox

    return list((await session.execute(select(Outbox))).scalars().all())


async def _notification(session, order_id: str = ORDER_ID):
    return await NotificationPostgresAdapter(session).obtener(order_id, 0)


async def test_spec_16_enqueue_and_send_confirmation_persists_everything(postgres_session):
    await _paid_order(postgres_session)
    sender = FakeEmailSender()
    service = _service(postgres_session, sender)

    notice = await service.enqueue_confirmation(ORDER_ID)
    assert notice["mensaje"].startswith("Te enviaremos la confirmación")
    pending = await _notification(postgres_session)
    assert pending is not None and pending.status == "PENDING"
    rows = await _outbox_rows(postgres_session)
    assert len(rows) == 1 and rows[0].payload["order_id"] == ORDER_ID

    result = await service.handle_send_email_task(
        type("Task", (), {"id": rows[0].id, "payload": rows[0].payload})()
    )
    assert result.outcome is SendOutcome.SENT
    sent = await _notification(postgres_session)
    assert sent.status == "SENT" and sent.attempts == 0
    rows = await _outbox_rows(postgres_session)
    assert rows[0].status == "PROCESSED"
    assert len(sender.sent) == 1
    assert ORDER_ID in sender.sent[0].subject
    assert "María Pérez" in sender.sent[0].text
    assert "maria@ejemplo.com" in sender.sent[0].to


async def test_spec_16_smtp_failure_is_retried_without_blocking_the_purchase(postgres_session):
    await _paid_order(postgres_session)
    sender = FakeEmailSender(fail_times=1)
    # send_inline models the request path: a failed SMTP call never raises.
    service = _service(postgres_session, sender, send_inline=True)

    notice = await service.enqueue_confirmation(ORDER_ID)
    assert notice["mensaje"]
    retried = await _notification(postgres_session)
    assert retried.status == "PENDING" and retried.attempts == 1
    assert "unavailable" in (retried.last_error or "")
    rows = await _outbox_rows(postgres_session)
    assert rows[0].status == "PENDING"
    assert rows[0].next_attempt_at > datetime.now(timezone.utc)

    # The retry the worker would run once the SMTP provider is back.
    result = await service.handle_send_email_task(
        type("Task", (), {"id": rows[0].id, "payload": rows[0].payload})()
    )
    assert result.outcome is SendOutcome.SENT
    assert (await _notification(postgres_session)).status == "SENT"
    assert (await _outbox_rows(postgres_session))[0].status == "PROCESSED"
    assert len(sender.sent) == 1


async def test_spec_16_three_failures_end_failed_and_never_raise(postgres_session):
    await _paid_order(postgres_session, order_id="PED-2026-00992")
    sender = FakeEmailSender(fail_times=4)
    service = _service(postgres_session, sender)
    await service.enqueue_confirmation("PED-2026-00992")
    task_row = (await _outbox_rows(postgres_session))[0]

    outcomes = []
    for _ in range(4):
        outcomes.append(await service.handle_send_email_task(
            type("Task", (), {"id": task_row.id, "payload": task_row.payload})()
        ))
    assert [outcome.outcome for outcome in outcomes] == [
        SendOutcome.RETRY, SendOutcome.RETRY, SendOutcome.RETRY, SendOutcome.FAILED,
    ]
    final = await _notification(postgres_session, "PED-2026-00992")
    assert final.status == "FAILED" and final.attempts == 3
    assert (await _outbox_rows(postgres_session))[0].status == "FAILED"


def _jwt(sub: uuid.UUID) -> str:
    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part({'sub': str(sub)})}.signature"


def _snapshot(customer_id: uuid.UUID, total: float) -> dict[str, Any]:
    return {
        "canal": "CHATBOT",
        "contacto": {"clienteId": str(customer_id)},
        "items": [{"productoId": "P-1", "sku": "SKU-1", "descripcion": "Zapatilla",
                   "cantidad": 1, "precioUnitario": total}],
        "envio": {"distrito": "Miraflores", "costo": 12.5},
        "pago": {"total": total},
    }


def _orders_api(sales: FakeSalesPort, customer_id: uuid.UUID = CUSTOMER_ID) -> TestClient:
    app = FastAPI()
    register_problem_handlers(app)
    service = OrdersService(notifications=None, sales_client=sales)  # type: ignore[arg-type]
    app.include_router(create_orders_router(lambda: service), prefix="/api/v1")
    app.dependency_overrides[get_caller_identity] = lambda: CallerIdentity(
        customer_id=customer_id, access_token=_jwt(customer_id))
    return TestClient(app, raise_server_exceptions=False)


async def test_spec_17_list_and_detail_of_the_customer_orders():
    sales = FakeSalesPort()
    own = (await sales.create_order(_snapshot(CUSTOMER_ID, 100.0), idempotency_key="k1"))["pedidoId"]
    await sales.create_order(_snapshot(CUSTOMER_ID, 50.0), idempotency_key="k2")
    await sales.create_order(_snapshot(OTHER_CUSTOMER, 70.0), idempotency_key="k3")
    client = _orders_api(sales)
    headers = {"Authorization": f"Bearer {_jwt(CUSTOMER_ID)}"}

    listed = client.get("/api/v1/pedidos", headers=headers)
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 2
    ids = {row["pedidoId"] for row in body["items"]}
    assert own in ids and len(ids) == 2
    assert all(row["estadoEtiqueta"] for row in body["items"])

    detail = client.get(f"/api/v1/pedidos/{own}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["pedidoId"] == own
    assert detail.json()["estadoEtiqueta"] == "Pendiente de pago"
    assert detail.json()["historial"][0]["etiqueta"] == "Pendiente de pago"


async def test_spec_17_unknown_and_foreign_orders_answer_not_found():
    sales = FakeSalesPort()
    (await sales.create_order(_snapshot(CUSTOMER_ID, 100.0), idempotency_key="k1"))["pedidoId"]
    foreign = (await sales.create_order(_snapshot(OTHER_CUSTOMER, 70.0), idempotency_key="k2"))["pedidoId"]
    client = _orders_api(sales)
    headers = {"Authorization": f"Bearer {_jwt(CUSTOMER_ID)}"}

    for order_id in ("PED-2026-NOEXISTE", foreign):
        response = client.get(f"/api/v1/pedidos/{order_id}", headers=headers)
        assert response.status_code == 404
        assert response.json()["code"] == "RECURSO_NO_ENCONTRADO"
        assert "No encontré ese pedido" in response.json()["detail"]
