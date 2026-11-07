"""`/pedidos` through the real OrdersService + FakeSalesPort (MODULES_MODE=fake in production)."""
import base64
import json
import uuid
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.adapters.inbound.http.dependencies.caller_identity import get_caller_identity
from src.adapters.inbound.http.orders_router import create_orders_router
from src.adapters.inbound.http.problem import register_problem_handlers
from src.adapters.outbound.fakes.fake_ventas import FakeSalesPort
from src.domain.value_objects.caller_identity import CallerIdentity
from src.infrastructure.config.container import OrdersService

CUSTOMER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _jwt(sub: str) -> str:
    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part({'sub': sub})}.signature"


HEADERS = {"Authorization": f"Bearer {_jwt(str(CUSTOMER_ID))}"}


def _snapshot() -> dict[str, Any]:
    return {
        "canal": "CHATBOT",
        "contacto": {"clienteId": str(CUSTOMER_ID)},
        "items": [
            {"productoId": "P-1", "sku": "SKU-1", "descripcion": "Zapatilla", "cantidad": 1, "precioUnitario": 99.9}
        ],
        "envio": {"distrito": "Miraflores", "costo": 12.5},
        "pago": {"total": 112.4},
    }


def _client(sales: FakeSalesPort) -> TestClient:
    app = FastAPI()
    register_problem_handlers(app)
    service = OrdersService(notifications=None, sales_client=sales)  # type: ignore[arg-type]
    app.include_router(create_orders_router(lambda: service), prefix="/api/v1")
    app.dependency_overrides[get_caller_identity] = lambda: CallerIdentity(customer_id=CUSTOMER_ID)
    return TestClient(app, raise_server_exceptions=False)


async def _create(sales: FakeSalesPort) -> str:
    created = await sales.create_order(_snapshot(), idempotency_key="k1")
    return created["pedidoId"]


def test_list_orders_with_fake_sales_returns_labelled_items() -> None:
    import asyncio

    sales = FakeSalesPort()
    order_id = asyncio.run(_create(sales))

    response = _client(sales).get("/api/v1/pedidos", headers=HEADERS)

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["pedidoId"] == order_id
    assert item["estadoEtiqueta"]


def test_order_detail_with_fake_sales_returns_labelled_history() -> None:
    import asyncio

    sales = FakeSalesPort()
    order_id = asyncio.run(_create(sales))

    response = _client(sales).get(f"/api/v1/pedidos/{order_id}", headers=HEADERS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["estadoEtiqueta"]
    assert body["historial"][0]["etiqueta"]


def test_order_missing_from_restarted_fake_is_404() -> None:
    response = _client(FakeSalesPort()).get("/api/v1/pedidos/PED-2026-FACFFE", headers=HEADERS)

    assert response.status_code == 404
    assert response.json()["code"] == "RECURSO_NO_ENCONTRADO"


def test_malformed_sales_payload_is_a_controlled_503_not_a_500() -> None:
    class BrokenSales(FakeSalesPort):
        async def list_customer_orders(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"items": [{"pedidoId": "PED-1"}], "total": 1, "page": 1, "pageSize": 10}

        async def get_customer_order(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"pedidoId": "PED-1"}

    client = _client(BrokenSales())
    for path in ("/api/v1/pedidos", "/api/v1/pedidos/PED-1"):
        response = client.get(path, headers=HEADERS)
        assert response.status_code == 503, path
        assert response.json()["code"] == "SERVICIO_NO_DISPONIBLE"
