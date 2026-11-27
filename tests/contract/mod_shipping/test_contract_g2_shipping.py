"""G2 Despacho contract: quote and tracking (Markdown contract at 2bd99a2).

The tracking assertion deliberately checks the raw client payload
(`hitos[].etiqueta`). The `mod_orders` tracking mapper belongs to #213/#82 and
is out of scope for this contract PR.
"""
import json

import httpx
import pytest
import respx

from src.modules.mod_shipping.adapters.despacho_client import DespachoClient
from src.shared.domain.module_errors import ModuleRequestError

BASE_URL = "http://mock-despacho:4013/api/v1"


class StubServiceTokens:
    def __init__(self) -> None:
        self.requests: list[dict[str, str | None]] = []

    async def get_token(self, *, correlation_id=None, scope=None, audience=None) -> str:
        self.requests.append({"scope": scope, "audience": audience})
        return "svc-token"


def make_client(tokens=None) -> DespachoClient:
    return DespachoClient(
        base_url=BASE_URL,
        service_token_provider=tokens or StubServiceTokens(),
        timeout_seconds=4.0,
    )


@respx.mock
@pytest.mark.asyncio
async def test_quote_posts_the_documented_destino_and_lineas(load_fixture):
    body = load_fixture("g2_cotizacion_exacta.json")
    route = respx.post(f"{BASE_URL}/cotizaciones").mock(
        return_value=httpx.Response(200, json=body)
    )
    tokens = StubServiceTokens()
    destino = {
        "direccion": "Av. Javier Prado Este 2465",
        "distrito": "San Borja",
        "codigoPostal": "15036",
    }
    lineas = [
        {"sku": "POL-NEG-M", "cantidad": 5},
        {"sku": "ZAP-RUN-42", "cantidad": 1},
    ]

    result = await make_client(tokens).quote_shipping(destino=destino, lineas=lineas)

    payload = json.loads(route.calls[0].request.content)
    assert payload == {"destino": destino, "lineas": lineas}
    assert result == body
    assert result["tipoCotizacion"] == "EXACTA"
    assert result["costoEnvio"] == 10.0
    assert result["fechaEstimadaEntrega"] == "2026-09-25"
    assert tokens.requests == [
        {"scope": "cotizaciones:calcular", "audience": "api-despacho"}
    ]


@respx.mock
@pytest.mark.asyncio
async def test_tracking_returns_the_raw_g2_payload_with_hito_etiquetas(load_fixture):
    body = load_fixture("g2_seguimiento.json")
    route = respx.get(
        f"{BASE_URL}/seguimientos/pedidos/PED-2026-00891"
    ).mock(return_value=httpx.Response(200, json=body))
    tokens = StubServiceTokens()

    result = await make_client(tokens).get_order_tracking("PED-2026-00891")

    assert result == body
    assert result["idPedido"] == "PED-2026-00891"
    assert result["estado"] == "EN_CAMINO"
    assert result["estadoEtiqueta"] == "En camino"
    assert [hito["etiqueta"] for hito in result["hitos"]] == [
        "En centro de despacho",
        "Asignado a repartidor",
        "En camino",
    ]
    assert [hito["estado"] for hito in result["hitos"]] == [
        "PENDIENTE_ASIGNACION",
        "ASIGNADO",
        "EN_CAMINO",
    ]
    assert route.calls[0].request.headers["authorization"] == "Bearer svc-token"
    assert tokens.requests == [
        {"scope": "seguimientos:leer", "audience": "api-despacho"}
    ]


@respx.mock
@pytest.mark.asyncio
async def test_tracking_404_is_absence_not_an_error(load_fixture):
    body = load_fixture("g2_error_despacho_no_encontrado.json")
    respx.get(f"{BASE_URL}/seguimientos/pedidos/PED-2026-99999").mock(
        return_value=httpx.Response(404, json=body)
    )

    result = await make_client().get_order_tracking("PED-2026-99999")

    assert result is None


@respx.mock
@pytest.mark.asyncio
async def test_quote_429_maps_the_documented_code_and_retry_after(load_fixture):
    body = load_fixture("g2_error_limite_cotizacion.json")
    respx.post(f"{BASE_URL}/cotizaciones").mock(
        return_value=httpx.Response(429, json=body, headers={"Retry-After": "60"})
    )

    with pytest.raises(ModuleRequestError) as exc:
        await make_client().quote_shipping(
            destino={"distrito": "San Borja"}, lineas=[{"sku": "POL-NEG-M", "cantidad": 1}]
        )

    assert exc.value.status == 429
    assert exc.value.code == "DESP_ERROR_LIMITE_COTIZACION"
    assert exc.value.retry_after == "60"


@respx.mock
@pytest.mark.asyncio
async def test_quote_400_maps_the_documented_code(load_fixture):
    body = load_fixture("g2_error_destino_requerido.json")
    respx.post(f"{BASE_URL}/cotizaciones").mock(
        return_value=httpx.Response(400, json=body)
    )

    with pytest.raises(ModuleRequestError) as exc:
        await make_client().quote_shipping(destino={}, lineas=[{"sku": "POL-NEG-M", "cantidad": 1}])

    assert exc.value.status == 400
    assert exc.value.code == "DESP_ERROR_DESTINO_REQUERIDO"
