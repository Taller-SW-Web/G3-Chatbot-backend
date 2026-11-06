"""NotificationService: order confirmation e-mail (SPEC-16), offline.

In-memory repositories, FakeEmailSender and the real Jinja2 templates.

Trazabilidad:
- SPEC-16 Req. 1 · Scenario: Correo enviado / Contenido fiel al pedido
- SPEC-16 Req. 2 · Scenario: Reproceso del evento / Proveedor SMTP caído
- SPEC-16 Req. 3 · Scenario: Confirmación en el chat sin correo / El cliente pide reenviar el correo
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.adapters.inbound.http.dependencies.caller_identity import get_caller_identity
from src.adapters.inbound.http.orders_router import create_orders_router
from src.adapters.inbound.http.problem import register_problem_handlers
from src.adapters.outbound.fakes.fake_email_sender import FakeEmailSender
from src.adapters.outbound.notificaciones.jinja_email_renderer import JinjaEmailRenderer
from src.application.use_cases.notification_service import (
    NotificationError,
    NotificationService,
    SendOutcome,
)
from src.domain.value_objects.caller_identity import CallerIdentity
from src.infrastructure.config.container import OrdersService
from src.ports.outbound.notification_repository_port import (
    ConfirmationSource,
    EmailTask,
    NotificationRecord,
)

ORDER_ID = "PED-2026-00891"
CUSTOMER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)

SUMMARY = {
    "lineas": [
        {"sku": "ZAP-RUN-42-NEG", "nombre": "Zapatilla Running X", "talla": "42", "color": "Negro",
         "cantidad": 1, "precio": 299.90},
    ],
    "subtotal": 299.90,
    "descuento": 30.00,
    "cupon": "BIENVENIDA30",
    "costoEnvio": 12.50,
    "total": 282.40,
    "plazoEstimadoDias": 2,
    "direccion": {"destinatario": "María Pérez", "distrito": "Miraflores",
                  "direccion": "Av. Larco 1234, dpto. 502", "referencia": "Frente al parque"},
    "contacto": {"nombreCompleto": "María Pérez", "tipoDocumento": "DNI", "numeroDocumento": "72458912",
                 "telefono": "+51999888777", "email": "maria@ejemplo.com"},
}


# -- in-memory ports ------------------------------------------------------------


class MemNotifications:
    def __init__(self):
        self.rows: dict[tuple[str, int], dict] = {}

    def _record(self, row):
        return NotificationRecord(**row)

    async def crear_si_no_existe(self, order_id, resend_number, recipient):
        key = (order_id, resend_number)
        self.rows.setdefault(key, {
            "order_id": order_id, "resend_number": resend_number, "recipient": recipient,
            "status": "PENDING", "attempts": 0, "last_error": None,
        })
        return self._record(self.rows[key])

    async def obtener(self, order_id, resend_number):
        row = self.rows.get((order_id, resend_number))
        return self._record(row) if row else None

    async def contar_reenvios(self, order_id):
        return sum(1 for (order, n) in self.rows if order == order_id and n > 0)

    async def marcar_enviada(self, order_id, resend_number):
        self.rows[(order_id, resend_number)]["status"] = "SENT"

    async def registrar_reintento(self, order_id, resend_number, last_error):
        row = self.rows[(order_id, resend_number)]
        row["attempts"] += 1
        assert row["attempts"] <= 3  # CHECK (attempts BETWEEN 0 AND 3)
        row["last_error"] = last_error

    async def cerrar_fallida(self, order_id, resend_number, last_error):
        row = self.rows[(order_id, resend_number)]
        row["status"] = "FAILED"
        row["last_error"] = last_error


class MemOutbox:
    def __init__(self):
        self.tasks: dict[uuid.UUID, dict] = {}

    async def encolar_envio(self, payload):
        task_id = uuid.uuid4()
        self.tasks[task_id] = {"payload": dict(payload), "status": "PENDING", "next_attempt_at": None,
                               "last_error": None}
        return EmailTask(id=task_id, payload=dict(payload))

    async def buscar_envio_por_clave(self, order_id, customer_id, idempotency_key):
        for task_id, task in self.tasks.items():
            p = task["payload"]
            if (p.get("order_id"), p.get("customer_id"), p.get("idempotency_key")) == (
                order_id, customer_id, idempotency_key
            ):
                return EmailTask(id=task_id, payload=p)
        return None

    async def marcar_procesado(self, id):
        self.tasks[id]["status"] = "PROCESSED"

    async def marcar_reintento(self, id, next_attempt_at, last_error=None):
        self.tasks[id].update(next_attempt_at=next_attempt_at, last_error=last_error)

    async def marcar_fallido(self, id, last_error):
        self.tasks[id].update(status="FAILED", last_error=last_error)

    def task(self, resend_number=0):
        return next(
            EmailTask(id=task_id, payload=t["payload"])
            for task_id, t in self.tasks.items()
            if t["payload"]["resend_number"] == resend_number
        )


class MemSources:
    def __init__(self, status="PAID_NOTIFIED", summary=None):
        self.source = ConfirmationSource(
            order_id=ORDER_ID, customer_id=CUSTOMER_ID, local_status=status,
            created_at=NOW, summary=summary or SUMMARY, card_brand="VISA", card_last4="1111",
        )

    async def cargar(self, order_id):
        return self.source if order_id == ORDER_ID else None


class MemUow:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


def make_service(*, sender=None, inline=True, status="PAID_NOTIFIED"):
    notifications, outbox = MemNotifications(), MemOutbox()
    sender = sender or FakeEmailSender()
    service = NotificationService(
        notifications, outbox, MemSources(status=status), sender, JinjaEmailRenderer(), MemUow(),
        frontend_base_url="https://g3-chatbot-frontend.vercel.app/", send_inline=inline, clock=lambda: NOW,
    )
    return service, notifications, outbox, sender


# -- Req. 1 ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correo_enviado():
    service, notifications, outbox, sender = make_service()

    await service.enqueue_confirmation(ORDER_ID)

    assert len(sender.sent) == 1
    message = sender.sent[0]
    assert message.to == "maria@ejemplo.com"
    assert message.subject == "Confirmamos tu pedido PED-2026-00891"
    assert notifications.rows[(ORDER_ID, 0)]["status"] == "SENT"
    assert outbox.task(0).payload == {"order_id": ORDER_ID, "resend_number": 0}
    assert outbox.tasks[outbox.task(0).id]["status"] == "PROCESSED"


@pytest.mark.asyncio
async def test_contenido_fiel_al_pedido():
    service, _, _, sender = make_service()

    await service.enqueue_confirmation(ORDER_ID)

    message = sender.sent[0]
    for body in (message.html, message.text):
        assert "Zapatilla Running X" in body
        assert "42 / Negro" in body
        assert "S/ 299.90" in body
        assert "- S/ 30.00" in body and "BIENVENIDA30" in body
        assert "S/ 12.50" in body
        assert "S/ 282.40" in body
        assert "Visa •••• 1111" in body
        assert "Av. Larco 1234, dpto. 502" in body and "Miraflores" in body
        assert "2 días hábiles" in body
        assert "https://g3-chatbot-frontend.vercel.app/pedidos/PED-2026-00891" in body
        # RNF Privacidad: no document and no full phone number.
        assert "72458912" not in body
        assert "999888777" not in body
    assert 'width="600"' in message.html and "max-width:600px" in message.html


@pytest.mark.asyncio
async def test_html_escapa_datos_del_pedido():
    summary = {**SUMMARY, "lineas": [{"nombre": "<script>x</script>", "cantidad": 1, "precio": 10}]}
    service, _, _, sender = make_service()
    service._sources = MemSources(summary=summary)

    await service.enqueue_confirmation(ORDER_ID)

    assert "<script>" not in sender.sent[0].html
    assert "&lt;script&gt;" in sender.sent[0].html


# -- Req. 2 ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reproceso_del_evento():
    service, _, outbox, sender = make_service()
    await service.enqueue_confirmation(ORDER_ID)

    result = await service.handle_send_email_task(outbox.task(0))
    await service.enqueue_confirmation(ORDER_ID)
    direct = await service.send_confirmation(ORDER_ID)

    assert result.outcome is SendOutcome.ALREADY_SENT
    assert direct.outcome is SendOutcome.ALREADY_SENT
    assert sender.calls == 1
    assert len(outbox.tasks) == 1


@pytest.mark.asyncio
async def test_proveedor_smtp_caido_reintenta_1_5_15_y_queda_failed():
    sender = FakeEmailSender(fail_times=10)
    service, notifications, outbox, _ = make_service(sender=sender, inline=False)
    await service.enqueue_confirmation(ORDER_ID)
    task = outbox.task(0)

    results = [await service.handle_send_email_task(task) for _ in range(4)]

    assert [r.outcome for r in results] == [SendOutcome.RETRY] * 3 + [SendOutcome.FAILED]
    assert [r.next_attempt_at - NOW for r in results[:3]] == [
        timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15)
    ]
    row = notifications.rows[(ORDER_ID, 0)]
    assert row["status"] == "FAILED" and row["attempts"] == 3
    assert "fake SMTP unavailable" in row["last_error"]
    assert outbox.tasks[task.id]["status"] == "FAILED"
    assert sender.calls == 4 and sender.sent == []


@pytest.mark.asyncio
async def test_proveedor_se_recupera_en_un_reintento():
    sender = FakeEmailSender(fail_times=1)
    service, notifications, outbox, _ = make_service(sender=sender, inline=False)
    await service.enqueue_confirmation(ORDER_ID)
    task = outbox.task(0)

    first = await service.handle_send_email_task(task)
    second = await service.handle_send_email_task(task)

    assert (first.outcome, second.outcome) == (SendOutcome.RETRY, SendOutcome.SENT)
    assert notifications.rows[(ORDER_ID, 0)]["status"] == "SENT"
    assert len(sender.sent) == 1


# -- Req. 3 ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmacion_en_el_chat_sin_correo():
    service, notifications, _, sender = make_service(inline=False)

    result = await service.enqueue_confirmation(ORDER_ID)

    assert result["mensaje"] == "Te enviaremos la confirmación a m***a@…"
    assert result["correoEnmascarado"] == "m***a@ejemplo.com"
    assert notifications.rows[(ORDER_ID, 0)]["status"] == "PENDING"
    assert sender.sent == []


@pytest.mark.asyncio
async def test_correo_caido_no_bloquea_la_compra():
    service, notifications, _, _ = make_service(sender=FakeEmailSender(fail_times=1))

    result = await service.enqueue_confirmation(ORDER_ID)

    assert result["mensaje"].startswith("Te enviaremos la confirmación a ")
    assert notifications.rows[(ORDER_ID, 0)]["status"] == "PENDING"


@pytest.mark.asyncio
async def test_el_cliente_pide_reenviar_el_correo_maximo_2():
    service, notifications, _, sender = make_service()
    await service.enqueue_confirmation(ORDER_ID)

    first = await service.resend_confirmation(CUSTOMER_ID, ORDER_ID, idempotency_key="k1")
    second = await service.resend_confirmation(CUSTOMER_ID, ORDER_ID, idempotency_key="k2")
    with pytest.raises(NotificationError) as exc:
        await service.resend_confirmation(CUSTOMER_ID, ORDER_ID, idempotency_key="k3")

    assert first == {"ok": True, "pedidoId": ORDER_ID, "reenviosRestantes": 1,
                     "correoEnmascarado": "m***a@ejemplo.com"}
    assert second["reenviosRestantes"] == 0
    assert exc.value.code == "LIMITE_REENVIOS_EXCEDIDO"
    assert {n for (_, n) in notifications.rows} == {0, 1, 2}
    assert len(sender.sent) == 3


@pytest.mark.asyncio
async def test_reenvio_con_la_misma_clave_no_envia_otro_correo():
    service, _, outbox, sender = make_service()
    await service.enqueue_confirmation(ORDER_ID)

    first = await service.resend_confirmation(CUSTOMER_ID, ORDER_ID, idempotency_key="k1")
    repeated = await service.resend_confirmation(CUSTOMER_ID, ORDER_ID, idempotency_key="k1")

    assert repeated == first
    assert len(sender.sent) == 2
    assert len(outbox.tasks) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("customer, status", [
    (uuid.UUID("99999999-2222-3333-4444-555555555555"), "PAID_NOTIFIED"),
    (CUSTOMER_ID, "CREATED"),
])
async def test_reenvio_de_pedido_ajeno_o_sin_pagar(customer, status):
    service, _, _, _ = make_service(status=status)

    with pytest.raises(NotificationError) as exc:
        await service.resend_confirmation(customer, ORDER_ID, idempotency_key="k1")

    assert exc.value.code == "RECURSO_NO_ENCONTRADO"


# -- HTTP: POST /pedidos/{id}/reenviar-confirmacion -----------------------------------


def _client(service: NotificationService) -> TestClient:
    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(create_orders_router(lambda: OrdersService(service)), prefix="/api/v1")
    app.dependency_overrides[get_caller_identity] = lambda: CallerIdentity(customer_id=CUSTOMER_ID)
    return TestClient(app)


def test_endpoint_reenvio_202_y_429():
    service, _, _, _ = make_service()
    client = _client(service)
    url = f"/api/v1/pedidos/{ORDER_ID}/reenviar-confirmacion"
    auth = {"Authorization": "Bearer token"}

    responses = [client.post(url, headers={**auth, "Idempotency-Key": f"k{i}"}) for i in range(3)]

    assert [r.status_code for r in responses] == [202, 202, 429]
    assert responses[0].json() == {"ok": True, "pedidoId": ORDER_ID, "reenviosRestantes": 1,
                                   "correoEnmascarado": "m***a@ejemplo.com"}
    assert responses[2].json()["code"] == "LIMITE_REENVIOS_EXCEDIDO"
    assert client.post(url, headers=auth).json()["code"] == "VALIDACION"


def test_endpoint_reenvio_pedido_ajeno_404():
    service, _, _, _ = make_service(status="CREATED")
    response = _client(service).post(
        f"/api/v1/pedidos/{ORDER_ID}/reenviar-confirmacion",
        headers={"Authorization": "Bearer token", "Idempotency-Key": "k1"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "RECURSO_NO_ENCONTRADO"


def test_consultas_de_pedidos_pendientes_responden_503():
    service, _, _, _ = make_service()
    response = _client(service).get("/api/v1/pedidos", headers={"Authorization": "Bearer token"})
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICIO_NO_DISPONIBLE"
