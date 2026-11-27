"""G5 Ventas contract: customer orders and payment notification.

Pinned contract: `ventas.yaml` 1.10.0 at G5-Ventas-Postventas-Backend#41516ef.
The documented `ultimosCuatroDigitos` is optional and travels verbatim.

Exception: address `esPredeterminada` and order `csatEnviado` are not exercised
here because they depend on the pending alignment in #213. See the README.
"""
import json
import uuid

import httpx
import pytest
import respx

from src.shared.domain.module_errors import ModuleRequestError
from src.shared.infrastructure.http.ventas_client import VentasClient

BASE_URL = "http://mock-ventas:4012"


class StubServiceTokens:
    def __init__(self) -> None:
        self.requests: list[dict[str, str | None]] = []

    async def get_token(
        self, *, correlation_id=None, scope=None, audience=None
    ) -> str:
        self.requests.append({"scope": scope, "audience": audience})
        return "svc-token"


def make_client(tokens=None) -> VentasClient:
    return VentasClient(
        base_url=BASE_URL,
        timeout_seconds=5.0,
        service_token_provider=tokens or StubServiceTokens(),
    )


@respx.mock
@pytest.mark.asyncio
async def test_list_my_orders_uses_customer_bearer_and_documented_params(load_fixture):
    body = load_fixture("g5_pedidos_me.json")
    route = respx.get(f"{BASE_URL}/api/v1/pedidos/me").mock(
        return_value=httpx.Response(200, json=body)
    )

    result = await make_client().list_customer_orders(
        "holder-token",
        estado="PAGADO",
        desde="2026-11-01",
        hasta="2026-11-30",
        page=2,
        page_size=5,
    )

    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer holder-token"
    assert dict(request.url.params) == {
        "page": "2",
        "pageSize": "5",
        "estado": "PAGADO",
        "desde": "2026-11-01",
        "hasta": "2026-11-30",
    }
    assert result == body
    assert result["items"][0]["pedidoId"] == "PED-2026-00981"


@respx.mock
@pytest.mark.asyncio
async def test_order_detail_uses_customer_bearer(load_fixture):
    body = load_fixture("g5_pedido_detalle.json")
    route = respx.get(f"{BASE_URL}/api/v1/pedidos/PED-2026-00981").mock(
        return_value=httpx.Response(200, json=body)
    )

    result = await make_client().get_customer_order("PED-2026-00981", " holder-token ")

    assert route.calls[0].request.headers["authorization"] == "Bearer holder-token"
    assert result == body
    assert result["pago"]["estado"] == "CONFIRMADO"
    assert result["historialEstados"][-1]["actor"] == "PASARELA_PAGOS"


@respx.mock
@pytest.mark.asyncio
async def test_create_order_sends_documented_snapshot_idempotency_and_service_token(load_fixture):
    snapshot = load_fixture("g5_crear_pedido_request.json")
    body = load_fixture("g5_crear_pedido_response.json")
    route = respx.post(f"{BASE_URL}/api/v1/pedidos").mock(
        return_value=httpx.Response(201, json=body)
    )
    tokens = StubServiceTokens()
    idempotency_key = str(uuid.uuid4())

    result = await make_client(tokens).create_order(
        snapshot, idempotency_key=idempotency_key
    )

    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer svc-token"
    assert request.headers["idempotency-key"] == idempotency_key
    assert json.loads(request.content) == snapshot
    assert result == body
    assert result["estado"] == "CREADO"
    assert tokens.requests == [{"scope": "ventas:pedidos:crear", "audience": "api-ventas"}]


@respx.mock
@pytest.mark.asyncio
async def test_payment_notification_forwards_optional_card_digits(load_fixture):
    payload = load_fixture("g5_notificacion_pago_request.json")
    body = load_fixture("g5_notificacion_pago_response.json")
    route = respx.post(
        f"{BASE_URL}/api/v1/pedidos/PED-2026-00981/pagos/notificacion"
    ).mock(return_value=httpx.Response(200, json=body))
    tokens = StubServiceTokens()

    result = await make_client(tokens).notify_payment("PED-2026-00981", payload)

    request = route.calls[0].request
    sent = json.loads(request.content)
    assert request.headers["authorization"] == "Bearer svc-token"
    assert sent == payload
    assert sent["ultimosCuatroDigitos"] == "4242"
    assert result == body
    assert result["nuevoEstado"] == "PAGADO"
    assert tokens.requests == [
        {"scope": "ventas:pagos:webhook", "audience": "api-ventas"}
    ]


@respx.mock
@pytest.mark.asyncio
async def test_payment_notification_works_without_optional_card_digits(load_fixture):
    payload = load_fixture("g5_notificacion_pago_request.json")
    payload.pop("ultimosCuatroDigitos")
    body = load_fixture("g5_notificacion_pago_response.json")
    route = respx.post(
        f"{BASE_URL}/api/v1/pedidos/PED-2026-00981/pagos/notificacion"
    ).mock(return_value=httpx.Response(200, json=body))

    await make_client().notify_payment("PED-2026-00981", payload)

    assert "ultimosCuatroDigitos" not in json.loads(route.calls[0].request.content)


@respx.mock
@pytest.mark.asyncio
async def test_customer_order_404_maps_the_contract_code(load_fixture):
    body = load_fixture("g5_error_pedido_no_encontrado.json")
    respx.get(f"{BASE_URL}/api/v1/pedidos/PED-2026-99999").mock(
        return_value=httpx.Response(404, json=body)
    )

    with pytest.raises(ModuleRequestError) as exc:
        await make_client().get_customer_order("PED-2026-99999", "holder-token")

    assert exc.value.status == 404
    assert exc.value.code == "PEDIDO_NO_ENCONTRADO"
