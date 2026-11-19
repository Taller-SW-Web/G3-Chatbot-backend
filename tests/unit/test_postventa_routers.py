"""HTTP contract tests for claims, returns and CSAT routers."""
import uuid

import anyio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.adapters.inbound.http.claims_router import create_claims_router
from src.adapters.inbound.http.csat_router import create_csat_router
from src.adapters.inbound.http.dependencies.auth_guard import require_customer
from src.adapters.inbound.http.problem import register_problem_handlers
from src.adapters.inbound.http.returns_router import create_returns_router
from src.adapters.outbound.fakes.fake_postventa import FakePostventaPort
from src.adapters.outbound.fakes.fake_ventas import FakeSalesPort
from src.application.use_cases.postventa_service import PostventaService
from src.domain.value_objects.caller_identity import CallerIdentity

CUSTOMER = uuid.UUID("11111111-1111-1111-1111-111111111111")
HOLDER = f"fake-access-{CUSTOMER}"


class MemoryRefs:
    def __init__(self):
        self.rows = {}

    async def guardar(self, row):
        self.rows[row.idempotency_key] = row
        return row

    async def obtener_por_idempotency_key(self, key):
        return self.rows.get(key)


def _service() -> PostventaService:
    return PostventaService(
        postventa=FakePostventaPort(),
        sales=FakeSalesPort(),
        claim_refs=MemoryRefs(),
        return_refs=MemoryRefs(),
    )


def _app(service: PostventaService) -> TestClient:
    def provider():
        return service

    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(create_claims_router(provider), prefix="/api/v1")
    app.include_router(create_returns_router(provider), prefix="/api/v1")
    app.include_router(create_csat_router(provider), prefix="/api/v1")
    app.dependency_overrides[require_customer] = lambda: CallerIdentity(customer_id=CUSTOMER)
    app.dependency_overrides[provider] = lambda: service
    return TestClient(app, raise_server_exceptions=False)


def _order(service: PostventaService, customer: str = str(CUSTOMER), key: str = "order-key") -> str:
    async def create():
        return await service._sales.create_order(
            {
                "canal": "CHATBOT",
                "contacto": {"clienteId": customer, "nombreCompleto": "Ana"},
                "items": [{"productoId": "P-1", "sku": "SKU-1", "cantidad": 1, "precioUnitario": 10}],
                "envio": {"distrito": "Miraflores", "costo": 0},
                "pago": {"total": 10},
            },
            idempotency_key=key,
        )

    return anyio.run(create)["pedidoId"]


def _headers(extra=None):
    headers = {"Authorization": f"Bearer {HOLDER}"}
    if extra:
        headers.update(extra)
    return headers


def test_create_claim_returns_detail_and_requires_idempotency_key():
    service = _service()
    order_id = _order(service)
    client = _app(service)
    body = {
        "pedidoId": order_id,
        "tipo": "RECLAMO",
        "motivo": "PRODUCTO_DEFECTUOSO",
        "detalle": "La suela se despegó al primer uso.",
        "consumidor": {
            "nombreCompleto": "Ana",
            "documento": "72458912",
            "email": "ana@example.com",
            "telefono": "+51999888777",
        },
    }

    missing = client.post("/api/v1/reclamos", headers=_headers(), json=body)
    assert missing.status_code == 400
    assert missing.json()["code"] == "VALIDACION"

    response = client.post(
        "/api/v1/reclamos",
        headers=_headers({"Idempotency-Key": "claim-http-1"}),
        json=body,
    )
    assert response.status_code == 201, response.text
    assert response.json()["codigoSeguimiento"].startswith("REC-")


def test_claim_on_foreign_order_is_404():
    service = _service()
    foreign_id = _order(
        service,
        customer="22222222-2222-2222-2222-222222222222",
        key="order-key-2",
    )
    client = _app(service)
    response = client.post(
        "/api/v1/reclamos",
        headers=_headers({"Idempotency-Key": "claim-http-2"}),
        json={
            "pedidoId": foreign_id,
            "tipo": "RECLAMO",
            "motivo": "OTRO",
            "detalle": "No recibí el pedido y quiero reclamar.",
            "consumidor": {
                "nombreCompleto": "Ana",
                "documento": "72458912",
                "email": "ana@example.com",
                "telefono": "+51999888777",
            },
        },
    )
    assert response.status_code == 404


def test_create_return_and_list_returns():
    service = _service()
    client = _app(service)
    body = {
        "pedidoId": "PED-2026-00001",
        "tipo": "CAMBIO",
        "motivo": "TALLA_INCORRECTA",
        "items": [{"productoId": "P-1", "cantidad": 1}],
        "evidencias": [],
    }
    created = client.post(
        "/api/v1/devoluciones",
        headers=_headers({"Idempotency-Key": "return-http-1"}),
        json=body,
    )
    assert created.status_code == 201, created.text
    assert created.json()["devolucionId"].startswith("DEV-")

    listing = client.get("/api/v1/devoluciones", headers=_headers())
    assert listing.status_code == 200
    assert listing.json()["total"] == 1


def test_csat_requires_delivered_order():
    service = _service()
    order_id = _order(service)
    client = _app(service)

    response = client.post(
        f"/api/v1/pedidos/{order_id}/csat",
        headers=_headers(),
        json={"puntuacion": 5, "comentario": "Excelente"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "ORDER_NOT_DELIVERED"

    service._sales.orders[order_id]["estado"] = "ENTREGADO"
    response = client.post(
        f"/api/v1/pedidos/{order_id}/csat",
        headers=_headers(),
        json={"puntuacion": 5, "comentario": "Excelente"},
    )
    assert response.status_code == 201
