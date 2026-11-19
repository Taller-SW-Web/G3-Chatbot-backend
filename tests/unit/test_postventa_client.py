"""Tests for the G5 M2 Postventa client (F3, F5, F6)."""
import anyio
import httpx
import pytest
import respx

from src.adapters.outbound.http_clients.postventa_client import PostventaClient
from src.ports.outbound.module_errors import ModuleRequestError

BASE_URL = "http://mock-postventa:8082"
HOLDER = "fake-access-customer-1"


@respx.mock
def test_upload_evidence_sends_customer_token_and_multipart():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(
            201,
            json={
                "tipo": "DOCUMENTO",
                "url": "https://g5.example/evidencias/ev-1.pdf",
                "nombreArchivoOriginal": "ev-1.pdf",
                "tamanioBytes": 3,
                "fechaSubida": "2026-11-17T10:00:00Z",
            },
        )

    respx.post(f"{BASE_URL}/api/v2/devoluciones/evidencias/upload").mock(side_effect=handler)
    client = PostventaClient(base_url=BASE_URL, timeout_seconds=5)

    async def run():
        return await client.upload_evidence(
            file_name="ev-1.pdf",
            content_type="application/pdf",
            content=b"pdf",
            holder_token=HOLDER,
        )

    result = anyio.run(run)
    assert result["tipo"] == "DOCUMENTO"
    assert seen["authorization"] == f"Bearer {HOLDER}"


@respx.mock
def test_create_claim_uses_customer_token_and_normalizes_codigo():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(
            201,
            json={
                "reclamoId": "REC-2026-0015",
                "codigoSeguimiento": "REC-2026-0015",
                "estado": "REGISTRADO",
                "motivo": "PRODUCTO_DEFECTUOSO_O_INCORRECTO",
                "plazoDiasHabiles": 15,
                "fechaLimiteSLA": "2026-12-08T23:59:59Z",
                "respuestaVisibleCliente": None,
            },
        )

    respx.post(f"{BASE_URL}/api/v2/reclamos").mock(side_effect=handler)
    client = PostventaClient(base_url=BASE_URL, timeout_seconds=5)

    async def run():
        return await client.create_claim(
            {"pedidoId": "PED-2026-00001", "tipo": "RECLAMO"},
            holder_token=HOLDER,
        )

    result = anyio.run(run)
    assert result["codigoSeguimiento"] == "REC-2026-0015"
    assert seen["authorization"] == f"Bearer {HOLDER}"


@respx.mock
def test_list_returns_translates_page_and_type():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "contenido": [],
                "pagina": 0,
                "totalPaginas": 1,
                "totalElementos": 0,
            },
        )

    respx.get(f"{BASE_URL}/api/v2/devoluciones/me").mock(side_effect=handler)
    client = PostventaClient(base_url=BASE_URL, timeout_seconds=5)

    async def run():
        return await client.list_customer_returns(
            HOLDER, estado="SOLICITADA", tipo="CAMBIO", page=1, page_size=10
        )

    anyio.run(run)
    assert "pagina=0" in seen["url"]
    assert "tamano=10" in seen["url"]
    assert "estado=SOLICITADA" in seen["url"]
    assert "tipo=CAMBIO" in seen["url"]


@respx.mock
def test_postventa_4xx_normalizes_codigo_to_code():
    respx.post(f"{BASE_URL}/api/v2/devoluciones").mock(
        return_value=httpx.Response(409, json={"codigo": "DEVOLUCION_EN_CURSO", "mensaje": "..."})
    )
    client = PostventaClient(base_url=BASE_URL, timeout_seconds=5)

    async def run():
        await client.create_return({}, holder_token=HOLDER)

    with pytest.raises(ModuleRequestError) as exc_info:
        anyio.run(run)
    assert exc_info.value.status == 409
    assert exc_info.value.code == "DEVOLUCION_EN_CURSO"
