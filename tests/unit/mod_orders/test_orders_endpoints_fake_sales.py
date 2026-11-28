"""`/pedidos` through the real OrdersService + FakeSalesPort (MODULES_MODE=fake in production)."""
import base64
import json
import uuid
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.modules.mod_orders.adapters.http.orders_router import create_orders_router
from src.modules.mod_orders.application.orders_service import OrdersService
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.domain.module_errors import ModuleRequestError
from src.shared.infrastructure.fakes.fake_ventas import FakeSalesPort
from src.shared.infrastructure.http.dependencies.caller_identity import get_caller_identity
from src.shared.infrastructure.http.problem import register_problem_handlers

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


def _client(sales: FakeSalesPort, order_refs: Any = None) -> TestClient:
    app = FastAPI()
    register_problem_handlers(app)
    service = OrdersService(notifications=None, sales_client=sales, order_refs=order_refs)  # type: ignore[arg-type]
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
    assert body["csatEnviado"] is False


def test_order_detail_exposes_csat_enviado_from_the_local_order_ref() -> None:
    import asyncio

    class CsatRefs:
        async def obtener_por_order_id(self, order_id: str) -> SimpleNamespace:
            return SimpleNamespace(csat_sent=True)

    sales = FakeSalesPort()
    order_id = asyncio.run(_create(sales))

    response = _client(sales, order_refs=CsatRefs()).get(
        f"/api/v1/pedidos/{order_id}", headers=HEADERS
    )

    assert response.status_code == 200, response.text
    assert response.json()["csatEnviado"] is True


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


def test_sales_429_is_a_429_with_retry_after() -> None:
    class RateLimitedSales(FakeSalesPort):
        async def list_customer_orders(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            error = ModuleRequestError("sales", 429, "HTTP_429")
            error.retry_after = "42"
            raise error

        async def get_customer_order(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            error = ModuleRequestError("sales", 429, "HTTP_429")
            error.retry_after = "42"
            raise error

    client = _client(RateLimitedSales())
    for path in ("/api/v1/pedidos", "/api/v1/pedidos/PED-1"):
        response = client.get(path, headers=HEADERS)
        assert response.status_code == 429, path
        assert response.json()["code"] == "DEMASIADAS_SOLICITUDES"
        assert response.headers["Retry-After"] == "42"


def test_sales_429_without_retry_after_uses_60_seconds() -> None:
    class RateLimitedSales(FakeSalesPort):
        async def list_customer_orders(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise ModuleRequestError("sales", 429, "HTTP_429")

    response = _client(RateLimitedSales()).get("/api/v1/pedidos", headers=HEADERS)

    assert response.status_code == 429
    assert response.json()["code"] == "DEMASIADAS_SOLICITUDES"
    assert response.headers["Retry-After"] == "60"


def test_list_orders_forwards_estado_desde_and_hasta() -> None:
    class RecordingSales(FakeSalesPort):
        def __init__(self) -> None:
            super().__init__()
            self.received: dict[str, Any] = {}

        async def list_customer_orders(self, holder_token: str, **kwargs: Any) -> dict[str, Any]:
            self.received = {"holder_token": holder_token, **kwargs}
            return {"items": [], "total": 0, "page": kwargs.get("page", 1), "pageSize": kwargs.get("page_size", 10)}

    sales = RecordingSales()
    response = _client(sales).get(
        "/api/v1/pedidos?estado=ENTREGADO&desde=2026-01-01&hasta=2026-12-31&page=2&pageSize=5",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.text
    assert sales.received["estado"] == "ENTREGADO"
    assert sales.received["desde"] == "2026-01-01"
    assert sales.received["hasta"] == "2026-12-31"
    assert sales.received["page"] == 2
    assert sales.received["page_size"] == 5
    assert sales.received["holder_token"] == HEADERS["Authorization"].removeprefix("Bearer ")


class _CountingSales(FakeSalesPort):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def list_customer_orders(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {"items": [], "total": 0, "page": 1, "pageSize": 10}


def test_invalid_desde_is_400_validacion_without_calling_sales() -> None:
    sales = _CountingSales()
    response = _client(sales).get("/api/v1/pedidos?desde=2026-13-40", headers=HEADERS)

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "VALIDACION"
    assert any(error["campo"] == "desde" for error in body["errores"])
    assert sales.calls == 0


def test_reversed_date_range_is_400_validacion_without_calling_sales() -> None:
    sales = _CountingSales()
    response = _client(sales).get(
        "/api/v1/pedidos?desde=2026-12-31&hasta=2026-01-01", headers=HEADERS
    )

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "VALIDACION"
    assert any(error["campo"] == "desde" for error in body["errores"])
    assert sales.calls == 0


def test_stock_cancelled_order_detail_and_tracking_explain_reason_and_refund() -> None:
    import asyncio

    sales = FakeSalesPort()
    order_id = asyncio.run(_create(sales))
    sales.cancel_for_stock(order_id)
    client = _client(sales)

    detail = client.get(f"/api/v1/pedidos/{order_id}", headers=HEADERS)
    tracking = client.get(f"/api/v1/pedidos/{order_id}/seguimiento", headers=HEADERS)

    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["estado"] == "ANULADO"
    assert body["motivoAnulacion"] == "STOCK_NO_DISPONIBLE"
    assert body["reembolso"] == "Se procesará el reembolso al medio de pago original."
    assert body["historial"][-1]["motivo"] == "STOCK_NO_DISPONIBLE"
    assert tracking.status_code == 200, tracking.text
    assert tracking.json()["mensaje"] == (
        "Tu pedido fue anulado por falta de stock. Se procesará el reembolso al medio de pago original."
    )
    assert tracking.json()["motivoAnulacion"] == "STOCK_NO_DISPONIBLE"


def test_open_order_detail_has_null_cancellation_fields() -> None:
    import asyncio

    sales = FakeSalesPort()
    order_id = asyncio.run(_create(sales))

    body = _client(sales).get(f"/api/v1/pedidos/{order_id}", headers=HEADERS).json()

    assert body["motivoAnulacion"] is None
    assert body["reembolso"] is None
