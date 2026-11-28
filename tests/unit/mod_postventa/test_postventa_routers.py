"""HTTP contract tests for claims, returns and CSAT routers."""
import uuid

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.modules.mod_postventa.adapters.fake_postventa import FakePostventaPort
from src.modules.mod_postventa.adapters.http.claims_router import create_claims_router
from src.modules.mod_postventa.adapters.http.csat_router import create_csat_router
from src.modules.mod_postventa.adapters.http.returns_router import create_returns_router
from src.modules.mod_postventa.application.postventa_dto import CLAIM_REASONS, CreateClaimRequest
from src.modules.mod_postventa.application.postventa_service import PostventaService
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.infrastructure.fakes.fake_ventas import FakeSalesPort
from src.shared.infrastructure.http.dependencies.auth_guard import require_customer
from src.shared.infrastructure.http.problem import register_problem_handlers

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
        "motivo": "PRODUCTO_DEFECTUOSO_O_INCORRECTO",
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
            "motivo": "INCUMPLIMIENTO_PLAZO_ENTREGA",
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


@pytest.mark.parametrize("motivo", sorted(CLAIM_REASONS))
def test_claim_accepts_the_five_g5_reasons(motivo: str) -> None:
    body = {
        "pedidoId": "PED-2026-90201",
        "tipo": "RECLAMO",
        "motivo": motivo,
        "detalle": "La suela se despegó al primer uso.",
        "consumidor": {
            "nombreCompleto": "Ana",
            "documento": "72458912",
            "email": "ana@example.com",
            "telefono": "+51999888777",
        },
    }

    assert CreateClaimRequest.model_validate(body).motivo == motivo


@pytest.mark.parametrize(
    "motivo",
    [
        "OTRO",
        "PRODUCTO_DEFECTUOSO",
        "PRODUCTO_EQUIVOCADO",
        "PEDIDO_INCOMPLETO",
        "NO_RECIBIDO",
        "COBRO_INCORRECTO",
        "ATENCION",
        "DESCONOCIDO",
    ],
)
def test_claim_rejects_legacy_or_unknown_reasons_with_400(motivo: str) -> None:
    service = _service()
    client = _app(service)

    response = client.post(
        "/api/v1/reclamos",
        headers=_headers({"Idempotency-Key": f"claim-invalid-{motivo}"}),
        json={
            "pedidoId": "PED-2026-90201",
            "tipo": "RECLAMO",
            "motivo": motivo,
            "detalle": "La suela se despegó al primer uso.",
            "consumidor": {
                "nombreCompleto": "Ana",
                "documento": "72458912",
                "email": "ana@example.com",
                "telefono": "+51999888777",
            },
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "VALIDACION"
    assert service._postventa.claims == {}


@pytest.mark.parametrize("puntuacion", [2, 4])
def test_csat_rejects_scores_outside_1_3_5(puntuacion: int) -> None:
    service = _service()
    client = _app(service)

    response = client.post(
        "/api/v1/pedidos/PED-2026-90201/csat",
        headers=_headers(),
        json={"puntuacion": puntuacion},
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "VALIDACION"


# -- B2: lenient response DTOs; B3: evidence ownership over HTTP; M2: logging -------


def test_return_detail_response_ignores_extra_g5_fields() -> None:
    from src.modules.mod_postventa.application.postventa_dto import ReturnDetailResponse

    detail = ReturnDetailResponse.model_validate(
        {
            "devolucionId": "DEV-1",
            "pedidoId": "PED-1",
            "tipo": "CAMBIO",
            "estado": "SOLICITADA",
            "motivo": "OTRO",
            "fechaRegistro": "2026-11-20T10:00:00Z",
            "items": [{"productoId": "P-1", "cantidad": 2, "sku": "SKU-1", "nombre": "Zapatilla"}],
            "evidencias": [
                {"tipo": "IMAGEN", "url": "https://g5.example/" + "a" * 600, "fechaSubida": "x"}
            ],
        }
    )

    assert detail.items[0].cantidad == 2
    assert len(detail.evidencias[0].url) > 500


def test_return_request_stays_strict() -> None:
    from pydantic import ValidationError

    from src.modules.mod_postventa.application.postventa_dto import CreateReturnRequest

    with pytest.raises(ValidationError):
        CreateReturnRequest.model_validate(
            {
                "pedidoId": "PED-1",
                "tipo": "CAMBIO",
                "motivo": "OTRO",
                "items": [{"productoId": "P-1", "cantidad": 1, "extra": True}],
            }
        )


class _OwnedConversations:
    def __init__(self, owned):
        self.owned = owned

    async def find_owned(self, conversation_id, owner):
        return object() if conversation_id in self.owned and owner.customer_id == CUSTOMER else None


class _CountingG5(FakePostventaPort):
    uploads = 0

    async def upload_evidence(self, **kwargs):
        self.uploads += 1
        return await super().upload_evidence(**kwargs)


def _upload_files():
    return {"file": ("foto.png", b"png-bytes", "image/png")}


def test_evidence_with_foreign_or_missing_conversation_is_404_and_skips_g5() -> None:
    own = uuid.uuid4()
    postventa = _CountingG5()
    service = PostventaService(
        postventa=postventa, sales=FakeSalesPort(), conversations=_OwnedConversations({own})
    )
    client = _app(service)

    for conversation in (uuid.uuid4(), uuid.uuid4()):
        response = client.post(
            "/api/v1/evidencias",
            headers=_headers(),
            files=_upload_files(),
            data={"conversacionId": str(conversation)},
        )
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "RECURSO_NO_ENCONTRADO"
    assert postventa.uploads == 0

    ok = client.post(
        "/api/v1/evidencias",
        headers=_headers(),
        files=_upload_files(),
        data={"conversacionId": str(own)},
    )
    assert ok.status_code == 201, ok.text
    assert postventa.uploads == 1


def test_unexpected_postventa_error_is_logged_and_503(caplog) -> None:
    class Boom(FakePostventaPort):
        async def get_customer_return(self, *args, **kwargs):
            raise RuntimeError("boom")

    service = PostventaService(postventa=Boom(), sales=FakeSalesPort())
    client = _app(service)
    with caplog.at_level("ERROR"):
        response = client.get("/api/v1/devoluciones/DEV-1", headers=_headers())

    assert response.status_code == 503
    assert any(r.exc_info and isinstance(r.exc_info[1], RuntimeError) for r in caplog.records)
    assert HOLDER not in caplog.text


# -- conversacionId in the HTTP forms (SPEC-19/21 drafts) ---------------------------


class _ReturnRecorder:
    def __init__(self):
        self.kwargs = None

    async def create_return(self, **kwargs):
        self.kwargs = kwargs
        return {
            "devolucionId": "DEV-2026-0001",
            "pedidoId": "PED-2026-00001",
            "tipo": "CAMBIO",
            "estado": "SOLICITADA",
            "motivo": "OTRO",
            "fechaRegistro": "2026-11-27T12:00:00Z",
        }


class _ClaimRecorder:
    def __init__(self):
        self.kwargs = None

    async def create_claim(self, **kwargs):
        self.kwargs = kwargs
        return {
            "reclamoId": "REC-2026-0001",
            "codigoSeguimiento": "REC-2026-0001",
            "pedidoId": "PED-2026-00001",
            "tipo": "RECLAMO",
            "motivo": "PRODUCTO_DEFECTUOSO_O_INCORRECTO",
            "detalle": "La suela se despegó al primer uso.",
            "estado": "REGISTRADO",
            "fechaLimiteSLA": "2026-12-18T12:00:00Z",
            "fechaRegistro": "2026-11-27T12:00:00Z",
        }


def _return_body() -> dict:
    return {
        "pedidoId": "PED-2026-00001",
        "tipo": "CAMBIO",
        "motivo": "TALLA_INCORRECTA",
        "items": [{"productoId": "P-1", "cantidad": 1}],
        "evidencias": [],
    }


def _claim_body() -> dict:
    return {
        "pedidoId": "PED-2026-00001",
        "tipo": "RECLAMO",
        "motivo": "PRODUCTO_DEFECTUOSO_O_INCORRECTO",
        "detalle": "La suela se despegó al primer uso.",
        "consumidor": {
            "nombreCompleto": "Ana",
            "documento": "72458912",
            "email": "ana@example.com",
            "telefono": "+51999888777",
        },
    }


@pytest.mark.parametrize("with_conversation", [True, False])
def test_create_return_passes_conversacion_id_and_never_forwards_it(with_conversation: bool) -> None:
    recorder = _ReturnRecorder()
    client = _app(recorder)
    conversation_id = uuid.uuid4()
    body = _return_body()
    if with_conversation:
        body["conversacionId"] = str(conversation_id)

    response = client.post(
        "/api/v1/devoluciones",
        headers=_headers({"Idempotency-Key": f"return-conv-{with_conversation}"}),
        json=body,
    )

    assert response.status_code == 201, response.text
    assert recorder.kwargs["conversation_id"] == (conversation_id if with_conversation else None)
    assert "conversacionId" not in recorder.kwargs["payload"]


@pytest.mark.parametrize("with_conversation", [True, False])
def test_create_claim_passes_conversacion_id_and_never_forwards_it(with_conversation: bool) -> None:
    recorder = _ClaimRecorder()
    client = _app(recorder)
    conversation_id = uuid.uuid4()
    body = _claim_body()
    if with_conversation:
        body["conversacionId"] = str(conversation_id)

    response = client.post(
        "/api/v1/reclamos",
        headers=_headers({"Idempotency-Key": f"claim-conv-{with_conversation}"}),
        json=body,
    )

    assert response.status_code == 201, response.text
    assert recorder.kwargs["conversation_id"] == (conversation_id if with_conversation else None)
    assert "conversacionId" not in recorder.kwargs["payload"]
