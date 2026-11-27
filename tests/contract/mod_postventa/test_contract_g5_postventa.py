"""G5 Postventa contract: evidence, returns, claims and CSAT.

Pinned contract: `postventa.yaml` 1.6.0 at G5-Ventas-Postventas-Backend#41516ef.

CSAT covers the documented 201 (no body), 403 ACCESO_DENEGADO and
404 PEDIDO_NO_ENCONTRADO, per the closed issue #84 decision.
"""
import json

import httpx
import pytest
import respx

from src.modules.mod_postventa.adapters.postventa_client import PostventaClient
from src.shared.domain.module_errors import ModuleRequestError

BASE_URL = "http://mock-postventa:8082"


def make_client() -> PostventaClient:
    return PostventaClient(base_url=BASE_URL, timeout_seconds=5.0)


@respx.mock
@pytest.mark.asyncio
async def test_upload_evidence_uses_the_documented_multipart_field(load_fixture):
    body = load_fixture("g5_evidencia_subida.json")
    route = respx.post(f"{BASE_URL}/api/v2/devoluciones/evidencias/upload").mock(
        return_value=httpx.Response(201, json=body)
    )

    result = await make_client().upload_evidence(
        file_name="evidencia.jpg",
        content_type="image/jpeg",
        content=b"fake-image-bytes",
        holder_token="holder-token",
    )

    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer holder-token"
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'filename="evidencia.jpg"' in request.content
    assert b"fake-image-bytes" in request.content
    assert result == body
    assert result["tipo"] == "IMAGEN"
    assert result["url"].endswith(".jpg")


@respx.mock
@pytest.mark.asyncio
async def test_create_return_sends_the_documented_request_and_bearer(load_fixture):
    payload = load_fixture("g5_devolucion_request.json")
    body = load_fixture("g5_devolucion_response.json")
    route = respx.post(f"{BASE_URL}/api/v2/devoluciones").mock(
        return_value=httpx.Response(201, json=body)
    )

    result = await make_client().create_return(payload, holder_token="holder-token")

    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer holder-token"
    assert json.loads(request.content) == payload
    assert result == body
    assert result["devolucionId"] == "DEV-2026-0042"


@respx.mock
@pytest.mark.asyncio
async def test_create_return_409_maps_the_contract_code(load_fixture):
    body = load_fixture("g5_error_devolucion_en_curso.json")
    respx.post(f"{BASE_URL}/api/v2/devoluciones").mock(
        return_value=httpx.Response(409, json=body)
    )

    with pytest.raises(ModuleRequestError) as exc:
        await make_client().create_return(
            {"pedidoId": "PED-2026-00981"}, holder_token="holder-token"
        )

    assert exc.value.status == 409
    assert exc.value.code == "DEVOLUCION_EN_CURSO"


@respx.mock
@pytest.mark.asyncio
async def test_list_my_returns_uses_zero_based_pagina(load_fixture):
    body = load_fixture("g5_devoluciones_listado.json")
    route = respx.get(f"{BASE_URL}/api/v2/devoluciones/me").mock(
        return_value=httpx.Response(200, json=body)
    )

    result = await make_client().list_customer_returns(
        "holder-token", estado="EN_EVALUACION", tipo="DEVOLUCION_DINERO", page=2
    )

    params = dict(route.calls[0].request.url.params)
    assert params == {
        "pagina": "1",
        "tamano": "10",
        "estado": "EN_EVALUACION",
        "tipo": "DEVOLUCION_DINERO",
    }
    assert result == body
    assert result["contenido"][0]["devolucionId"] == "DEV-2026-0042"


@respx.mock
@pytest.mark.asyncio
async def test_return_detail_reads_the_documented_expediente(load_fixture):
    body = load_fixture("g5_devolucion_detalle.json")
    route = respx.get(f"{BASE_URL}/api/v2/devoluciones/DEV-2026-0042").mock(
        return_value=httpx.Response(200, json=body)
    )

    result = await make_client().get_customer_return("DEV-2026-0042", "holder-token")

    assert route.calls[0].request.headers["authorization"] == "Bearer holder-token"
    assert result == body
    assert result["historial"][0]["actor"] == "CLIENTE"
    assert result["resolucion"] is None


@respx.mock
@pytest.mark.asyncio
async def test_create_claim_sends_the_documented_consumer_and_bearer(load_fixture):
    payload = load_fixture("g5_reclamo_request.json")
    body = load_fixture("g5_reclamo_response.json")
    route = respx.post(f"{BASE_URL}/api/v2/reclamos").mock(
        return_value=httpx.Response(201, json=body)
    )

    result = await make_client().create_claim(payload, holder_token="holder-token")

    assert route.calls[0].request.headers["authorization"] == "Bearer holder-token"
    assert json.loads(route.calls[0].request.content) == payload
    assert result == body
    assert result["codigoSeguimiento"] == "REC-77-2026"


@respx.mock
@pytest.mark.asyncio
async def test_list_my_claims_uses_zero_based_pagina(load_fixture):
    body = load_fixture("g5_reclamos_listado.json")
    route = respx.get(f"{BASE_URL}/api/v2/reclamos/me").mock(
        return_value=httpx.Response(200, json=body)
    )

    result = await make_client().list_customer_claims(
        "holder-token", estado="REGISTRADO", page=1
    )

    assert dict(route.calls[0].request.url.params) == {
        "pagina": "0",
        "tamano": "10",
        "estado": "REGISTRADO",
    }
    assert result == body
    assert result["contenido"][0]["estadoSla"] == "EN_PLAZO"


@respx.mock
@pytest.mark.asyncio
async def test_claim_detail_reads_the_documented_sla(load_fixture):
    body = load_fixture("g5_reclamo_detalle.json")
    route = respx.get(f"{BASE_URL}/api/v2/reclamos/REC-77-2026").mock(
        return_value=httpx.Response(200, json=body)
    )

    result = await make_client().get_claim("REC-77-2026", "holder-token")

    assert route.calls[0].request.headers["authorization"] == "Bearer holder-token"
    assert result == body
    assert result["plazoDiasHabiles"] == 15
    assert result["estadoSla"] == "EN_PLAZO"


@respx.mock
@pytest.mark.asyncio
async def test_csat_201_without_body_returns_empty_dict(load_fixture):
    payload = load_fixture("g5_csat_request.json")
    route = respx.post(f"{BASE_URL}/api/v2/csat").mock(
        return_value=httpx.Response(201)
    )

    result = await make_client().create_csat(payload, holder_token="holder-token")

    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer holder-token"
    assert json.loads(request.content) == payload
    assert payload["puntuacion"] == 5
    assert result == {}


@respx.mock
@pytest.mark.asyncio
async def test_csat_403_maps_acceso_denegado(load_fixture):
    payload = load_fixture("g5_csat_request.json")
    respx.post(f"{BASE_URL}/api/v2/csat").mock(
        return_value=httpx.Response(403, json={"code": "ACCESO_DENEGADO"})
    )

    with pytest.raises(ModuleRequestError) as exc:
        await make_client().create_csat(payload, holder_token="holder-token")

    assert exc.value.status == 403
    assert exc.value.code == "ACCESO_DENEGADO"


@respx.mock
@pytest.mark.asyncio
async def test_csat_404_maps_pedido_no_encontrado(load_fixture):
    payload = load_fixture("g5_csat_request.json")
    respx.post(f"{BASE_URL}/api/v2/csat").mock(
        return_value=httpx.Response(404, json={"code": "PEDIDO_NO_ENCONTRADO"})
    )

    with pytest.raises(ModuleRequestError) as exc:
        await make_client().create_csat(payload, holder_token="holder-token")

    assert exc.value.status == 404
    assert exc.value.code == "PEDIDO_NO_ENCONTRADO"
