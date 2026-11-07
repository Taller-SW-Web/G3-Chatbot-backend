"""Tests for VentasClient (SPEC-15, SPEC-17)."""
import anyio
import httpx
import pytest
import respx

from src.adapters.outbound.http_clients.ventas_client import VentasClient
from src.ports.outbound.module_errors import ModuleRequestError, ModuleUnavailableError
from src.ports.outbound.service_token_port import ServiceTokenProvider


class StubTokenProvider(ServiceTokenProvider):
    async def get_token(
        self,
        *,
        correlation_id: str | None = None,
        scope: str | None = None,
    ) -> str:
        return "fake-ventas-service-token"


BASE_URL = "http://mock-ventas:4012"


@respx.mock
def test_create_order_sends_service_token_and_idempotency_key():
    seen_headers = {}

    def handler(request):
        seen_headers.update(request.headers)
        return httpx.Response(
            201,
            json={
                "pedidoId": "PED-1234",
                "estado": "CREADO",
                "total": 299.90,
                "moneda": "PEN",
                "canal": "CHATBOT",
                "fechaCreacion": "2026-11-05T19:00:00Z",
            },
        )

    respx.post(f"{BASE_URL}/api/v1/pedidos").mock(side_effect=handler)

    client = VentasClient(
        base_url=BASE_URL,
        timeout_seconds=5.0,
        service_token_provider=StubTokenProvider(),
    )
    snapshot = {"canal": "CHATBOT", "pago": {"total": 299.90}}

    async def run():
        return await client.create_order(snapshot, idempotency_key="key-abc")

    res = anyio.run(run)
    assert res["pedidoId"] == "PED-1234"
    assert res["estado"] == "CREADO"
    assert seen_headers["authorization"] == "Bearer fake-ventas-service-token"
    assert seen_headers["idempotency-key"] == "key-abc"
    assert "x-correlation-id" in seen_headers


@respx.mock
def test_notify_payment_success():
    respx.post(f"{BASE_URL}/api/v1/pedidos/PED-1234/pagos/notificacion").mock(
        return_value=httpx.Response(
            200,
            json={
                "pedidoId": "PED-1234",
                "nuevoEstado": "PAGADO",
                "transaccionId": "SIM-999",
                "fechaTransicion": "2026-11-05T19:05:00Z",
            },
        )
    )

    client = VentasClient(
        base_url=BASE_URL,
        timeout_seconds=5.0,
        service_token_provider=StubTokenProvider(),
    )

    async def run():
        return await client.notify_payment("PED-1234", {"transaccionId": "SIM-999", "resultado": "APROBADO"})

    res = anyio.run(run)
    assert res["nuevoEstado"] == "PAGADO"


@respx.mock
def test_cancel_order_success():
    respx.post(f"{BASE_URL}/api/v1/pedidos/PED-1234/anulaciones").mock(
        return_value=httpx.Response(
            200,
            json={
                "pedidoId": "PED-1234",
                "estadoPedido": "ANULADO",
                "autorizacionRequerida": False,
                "solicitudReembolsoGenerada": False,
            },
        )
    )

    client = VentasClient(
        base_url=BASE_URL,
        timeout_seconds=5.0,
        service_token_provider=StubTokenProvider(),
    )

    async def run():
        return await client.cancel_order("PED-1234")

    res = anyio.run(run)
    assert res["estadoPedido"] == "ANULADO"


@respx.mock
def test_ventas_error_normalizes_codigo_to_code():
    respx.post(f"{BASE_URL}/api/v1/pedidos").mock(
        return_value=httpx.Response(409, json={"codigo": "SIN_STOCK", "mensaje": "Stock agotado"})
    )

    client = VentasClient(
        base_url=BASE_URL,
        timeout_seconds=5.0,
        service_token_provider=StubTokenProvider(),
    )

    async def run():
        await client.create_order({}, idempotency_key="k1")

    with pytest.raises(ModuleRequestError) as exc_info:
        anyio.run(run)

    assert exc_info.value.status == 409
    assert exc_info.value.code == "SIN_STOCK"


@respx.mock
def test_ventas_500_raises_unavailable():
    respx.post(f"{BASE_URL}/api/v1/pedidos").mock(
        return_value=httpx.Response(500, text="Internal Error")
    )

    client = VentasClient(
        base_url=BASE_URL,
        timeout_seconds=5.0,
        service_token_provider=StubTokenProvider(),
    )

    async def run():
        await client.create_order({}, idempotency_key="k2")

    with pytest.raises(ModuleUnavailableError):
        anyio.run(run)
