"""Despacho client (`SPEC-12`, `SPEC-18`): service token, quote and tracking.

No network: respx answers every call and a fake provider hands over the token,
so the test also pins the H4-S7 contract (`X-Correlation-Id` as a dashed UUID).
"""
import json
import uuid

import httpx
import pytest
import respx

from src.adapters.outbound.http_clients.base_client import ModuleUnavailableError
from src.adapters.outbound.http_clients.despacho_api_adapter import DespachoClient
from src.ports.outbound.module_errors import ModuleRequestError

BASE_URL = "http://mock-despacho:4013"
QUOTE = {
    "coberturaDisponible": True,
    "costoEnvio": 12.5,
    "moneda": "PEN",
    "plazoEstimadoDiasHabiles": 1,
    "fechaEstimadaEntrega": "2026-11-07",
    "nombreZona": "Lima Metropolitana",
}


class _Tokens:
    def __init__(self):
        self.calls = 0
        self.kwargs: list[dict[str, object]] = []

    async def get_token(
        self,
        *,
        correlation_id: str | None = None,
        scope: str | None = None,
        audience: str | None = None,
    ) -> str:
        self.calls += 1
        self.kwargs.append({"scope": scope, "audience": audience})
        return "svc-token"


def make_client(tokens=None) -> DespachoClient:
    return DespachoClient(
        base_url=BASE_URL,
        service_token_provider=tokens or _Tokens(),
        timeout_seconds=4.0,
    )


@respx.mock
@pytest.mark.asyncio
async def test_quote_sends_service_token_lines_and_dashed_correlation():
    route = respx.post(f"{BASE_URL}/cotizaciones").mock(
        return_value=httpx.Response(200, json=QUOTE)
    )
    tokens = _Tokens()

    result = await make_client(tokens).quote_shipping(
        destino={"distrito": "Miraflores", "direccion": "Av. Larco 1234"},
        lineas=[{"sku": "NIK-REV7-42-NEG", "cantidad": 1}],
    )

    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer svc-token"
    correlation = request.headers["x-correlation-id"]
    assert str(uuid.UUID(correlation)) == correlation
    payload = json.loads(request.content)
    assert payload["lineas"] == [{"sku": "NIK-REV7-42-NEG", "cantidad": 1}]
    assert payload["destino"]["distrito"] == "Miraflores"
    assert result["costoEnvio"] == 12.5
    assert result["plazoEstimadoDiasHabiles"] == 1
    # El token se pide con la audiencia y el scope que exige Despacho.
    assert tokens.kwargs == [
        {"scope": "cotizaciones:calcular", "audience": "api-despacho"}
    ]


@respx.mock
@pytest.mark.asyncio
async def test_quote_keeps_no_coverage_answer_as_data():
    respx.post(f"{BASE_URL}/cotizaciones").mock(
        return_value=httpx.Response(200, json={"coberturaDisponible": False})
    )

    result = await make_client().quote_shipping(destino={}, lineas=[{"sku": "X", "cantidad": 1}])

    assert result["coberturaDisponible"] is False


@respx.mock
@pytest.mark.asyncio
async def test_quote_maps_dispatch_error_code():
    respx.post(f"{BASE_URL}/cotizaciones").mock(
        return_value=httpx.Response(422, json={"codigo": "DESP_ERROR_LIMITE_COTIZACION"})
    )

    with pytest.raises(ModuleRequestError) as exc:
        await make_client().quote_shipping(destino={}, lineas=[{"sku": "X", "cantidad": 1}])

    assert exc.value.status == 422
    assert exc.value.code == "DESP_ERROR_LIMITE_COTIZACION"


@respx.mock
@pytest.mark.asyncio
async def test_quote_5xx_is_module_unavailable():
    respx.post(f"{BASE_URL}/cotizaciones").mock(return_value=httpx.Response(503))

    with pytest.raises(ModuleUnavailableError):
        await make_client().quote_shipping(destino={}, lineas=[{"sku": "X", "cantidad": 1}])


@respx.mock
@pytest.mark.asyncio
async def test_tracking_404_means_preparing_not_an_error():
    respx.get(f"{BASE_URL}/seguimientos/pedidos/PED-2026-00891").mock(
        return_value=httpx.Response(404)
    )

    assert await make_client().get_order_tracking("PED-2026-00891") is None


@respx.mock
@pytest.mark.asyncio
async def test_tracking_returns_body():
    respx.get(f"{BASE_URL}/seguimientos/pedidos/PED-2026-00891").mock(
        return_value=httpx.Response(
            200, json={"pedidoId": "PED-2026-00891", "estado": "EN_CAMINO"}
        )
    )

    result = await make_client().get_order_tracking("PED-2026-00891")

    assert result is not None
    assert result["estado"] == "EN_CAMINO"

@pytest.mark.asyncio
@respx.mock
async def test_tracking_requests_the_tracking_scope():
    respx.get(f"{BASE_URL}/seguimientos/pedidos/PED-1").mock(
        return_value=httpx.Response(200, json={"pedidoId": "PED-1"})
    )
    tokens = _Tokens()

    await make_client(tokens).get_order_tracking("PED-1")

    assert tokens.kwargs == [
        {"scope": "seguimientos:leer", "audience": "api-despacho"}
    ]


@respx.mock
@pytest.mark.asyncio
async def test_plain_text_429_is_a_rate_limit_not_a_module_outage():
    """Un 429 sin JSON (gateway/proxy) debe seguir siendo límite de peticiones."""
    respx.post(f"{BASE_URL}/cotizaciones").mock(
        return_value=httpx.Response(429, text="Too Many Requests")
    )

    with pytest.raises(ModuleRequestError) as exc:
        await make_client().quote_shipping(destino={"distrito": "Miraflores"},
                                           lineas=[{"sku": "X", "cantidad": 1}])

    assert exc.value.status == 429
    assert exc.value.code == "DESP_ERROR_LIMITE_COTIZACION"


@respx.mock
@pytest.mark.asyncio
async def test_429_keeps_retry_after_when_the_module_sends_it():
    respx.post(f"{BASE_URL}/cotizaciones").mock(
        return_value=httpx.Response(
            429,
            json={"codigo": "DESP_ERROR_LIMITE_COTIZACION"},
            headers={"Retry-After": "30"},
        )
    )

    with pytest.raises(ModuleRequestError) as exc:
        await make_client().quote_shipping(destino={"distrito": "Miraflores"},
                                           lineas=[{"sku": "X", "cantidad": 1}])

    assert exc.value.code == "DESP_ERROR_LIMITE_COTIZACION"
    assert exc.value.retry_after == "30"
