"""Tests for VentasClient (SPEC-15, SPEC-17)."""
import anyio
import httpx
import pytest
import respx

from src.shared.application.ports.service_token_port import ServiceTokenProvider
from src.shared.domain.module_errors import ModuleRequestError, ModuleUnavailableError
from src.shared.infrastructure.http.ventas_client import VentasClient


class StubTokenProvider(ServiceTokenProvider):
    def __init__(self) -> None:
        self.audiences: list[str | None] = []

    async def get_token(
        self,
        *,
        correlation_id: str | None = None,
        scope: str | None = None,
        audience: str | None = None,
    ) -> str:
        self.audiences.append(audience)
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

    tokens = StubTokenProvider()
    client = VentasClient(
        base_url=BASE_URL,
        timeout_seconds=5.0,
        service_token_provider=tokens,
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
    assert tokens.audiences == ["api-ventas"]


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
def test_get_order_as_service_uses_the_service_token_and_the_consult_scope():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"pedidoId": "PED-1234", "estado": "ANULADO"})

    respx.get(f"{BASE_URL}/api/v1/pedidos/PED-1234").mock(side_effect=handler)
    scopes: list[str | None] = []

    class ScopeStub(StubTokenProvider):
        async def get_token(self, *, correlation_id=None, scope=None, audience=None) -> str:
            scopes.append(scope)
            return await super().get_token(correlation_id=correlation_id, scope=scope, audience=audience)

    client = VentasClient(base_url=BASE_URL, timeout_seconds=5.0, service_token_provider=ScopeStub())

    async def run():
        return await client.get_order_as_service("PED-1234", correlation_id="c-1")

    res = anyio.run(run)
    assert res["estado"] == "ANULADO"
    assert seen["authorization"] == "Bearer fake-ventas-service-token"
    assert scopes == ["ventas:pedidos:consultar"]


@respx.mock
def test_get_order_as_service_surfaces_a_refused_read():
    respx.get(f"{BASE_URL}/api/v1/pedidos/PED-1").mock(
        return_value=httpx.Response(403, json={"codigo": "ACCESO_DENEGADO"})
    )
    client = VentasClient(base_url=BASE_URL, timeout_seconds=5.0, service_token_provider=StubTokenProvider())

    async def run():
        return await client.get_order_as_service("PED-1")

    with pytest.raises(ModuleRequestError) as exc:
        anyio.run(run)
    assert (exc.value.status, exc.value.code) == (403, "ACCESO_DENEGADO")


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
def test_ventas_429_preserves_retry_after():
    respx.post(f"{BASE_URL}/api/v1/pedidos").mock(
        return_value=httpx.Response(
            429,
            json={"codigo": "DEMASIADAS_SOLICITUDES"},
            headers={"Retry-After": "42"},
        )
    )

    client = VentasClient(
        base_url=BASE_URL,
        timeout_seconds=5.0,
        service_token_provider=StubTokenProvider(),
    )

    async def run():
        await client.create_order({}, idempotency_key="k-429")

    with pytest.raises(ModuleRequestError) as exc_info:
        anyio.run(run)

    assert exc_info.value.status == 429
    assert exc_info.value.code == "DEMASIADAS_SOLICITUDES"
    assert exc_info.value.retry_after == "42"


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
