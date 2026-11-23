"""Tests for PostventaService: ownership, idempotency and normalization."""
import uuid

import anyio
import pytest

from src.modules.mod_postventa.adapters.fake_postventa import FakePostventaPort
from src.modules.mod_postventa.application.postventa_dto import PostventaError
from src.modules.mod_postventa.application.postventa_service import PostventaService
from src.shared.infrastructure.fakes.fake_ventas import FakeSalesPort


class MemoryRefs:
    def __init__(self):
        self.rows = {}

    async def guardar(self, row):
        self.rows[row.idempotency_key] = row
        return row

    async def obtener_por_idempotency_key(self, key):
        return self.rows.get(key)


CUSTOMER = "11111111-1111-1111-1111-111111111111"
HOLDER = f"fake-access-{CUSTOMER}"


def _service():
    return PostventaService(
        postventa=FakePostventaPort(),
        sales=FakeSalesPort(),
        claim_refs=MemoryRefs(),
        return_refs=MemoryRefs(),
    )


async def _create_order(sales: FakeSalesPort, customer=CUSTOMER):
    return await sales.create_order(
        {
            "canal": "CHATBOT",
            "contacto": {"clienteId": customer, "nombreCompleto": "Ana"},
            "items": [{"productoId": "P-1", "sku": "SKU-1", "cantidad": 1, "precioUnitario": 10}],
            "envio": {"distrito": "Miraflores", "costo": 0},
            "pago": {"total": 10},
        },
        idempotency_key="order-key-1",
    )


def test_claim_is_linked_to_customer_and_idempotent():
    service = _service()
    sales = service._sales
    order = anyio.run(_create_order, sales)

    payload = {
        "pedidoId": order["pedidoId"],
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

    async def run():
        first = await service.create_claim(
            customer_id=CUSTOMER,
            holder_token=HOLDER,
            payload=payload,
            idempotency_key="claim-key-1",
        )
        second = await service.create_claim(
            customer_id=CUSTOMER,
            holder_token=HOLDER,
            payload=payload,
            idempotency_key="claim-key-1",
        )
        return first, second

    first, second = anyio.run(run)
    assert first["codigoSeguimiento"].startswith("REC-")
    assert first["codigoSeguimiento"] == second["codigoSeguimiento"]
    assert len(service._postventa.claims) == 1
    assert service._postventa.claim_payloads[0]["canal"] == "CHATBOT"


def test_claim_on_foreign_order_is_hidden():
    service = _service()
    sales = service._sales
    order = anyio.run(_create_order, sales, "22222222-2222-2222-2222-222222222222")

    payload = {
        "pedidoId": order["pedidoId"],
        "tipo": "RECLAMO",
        "motivo": "INCUMPLIMIENTO_PLAZO_ENTREGA",
        "detalle": "No recibí el pedido y quiero reclamar.",
        "consumidor": {
            "nombreCompleto": "Ana",
            "documento": "72458912",
            "email": "ana@example.com",
            "telefono": "+51999888777",
        },
    }

    async def run():
        await service.create_claim(
            customer_id=CUSTOMER,
            holder_token=HOLDER,
            payload=payload,
            idempotency_key="claim-key-2",
        )

    with pytest.raises(PostventaError) as exc_info:
        anyio.run(run)
    assert exc_info.value.code == "RECURSO_NO_ENCONTRADO"
    assert service._postventa.claims == {}


def test_return_is_idempotent_and_normalizes_pages():
    service = _service()
    payload = {
        "pedidoId": "PED-2026-00001",
        "tipo": "CAMBIO",
        "motivo": "TALLA_INCORRECTA",
        "items": [{"productoId": "P-1", "cantidad": 1}],
        "evidencias": [],
    }

    async def run():
        first = await service.create_return(
            customer_id=CUSTOMER,
            holder_token=HOLDER,
            payload=payload,
            idempotency_key="return-key-1",
        )
        second = await service.create_return(
            customer_id=CUSTOMER,
            holder_token=HOLDER,
            payload=payload,
            idempotency_key="return-key-1",
        )
        page = await service.list_returns(holder_token=HOLDER, page=1, page_size=10)
        return first, second, page

    first, second, page = anyio.run(run)
    assert first["devolucionId"] == second["devolucionId"]
    assert first["motivo"] == "OTRO"
    assert page["page"] == 1
    assert page["pageSize"] == 10
    assert len(page["items"]) == 1


def test_claim_reason_is_strict_even_outside_the_http_dto():
    service = _service()

    payload = {
        "tipo": "RECLAMO",
        "motivo": "OTRO",
        "detalle": "La suela se despegó al primer uso.",
        "consumidor": {
            "nombreCompleto": "Ana",
            "documento": "72458912",
            "email": "ana@example.com",
            "telefono": "+51999888777",
        },
    }

    async def run():
        await service.create_claim(
            customer_id=CUSTOMER,
            holder_token=HOLDER,
            payload=payload,
            idempotency_key="claim-invalid-service",
        )

    with pytest.raises(PostventaError) as exc_info:
        anyio.run(run)
    assert exc_info.value.code == "VALIDACION"
    assert service._postventa.claims == {}


def test_csat_requires_delivered_order():
    service = _service()
    sales = service._sales
    order = anyio.run(_create_order, sales)

    async def run():
        await service.create_csat(
            customer_id=CUSTOMER,
            holder_token=HOLDER,
            order_id=order["pedidoId"],
            payload={"puntuacion": 5},
        )

    with pytest.raises(PostventaError) as exc_info:
        anyio.run(run)
    assert exc_info.value.code == "ORDER_NOT_DELIVERED"

    sales.orders[order["pedidoId"]]["estado"] = "ENTREGADO"
    anyio.run(run)
    assert order["pedidoId"] in service._postventa.csat


@pytest.mark.parametrize("puntuacion", [2, 4])
def test_csat_rejects_unsupported_scores_even_in_the_service(puntuacion: int):
    service = _service()

    async def run():
        await service.create_csat(
            customer_id=CUSTOMER,
            holder_token=HOLDER,
            order_id="PED-2026-90201",
            payload={"puntuacion": puntuacion},
        )

    with pytest.raises(PostventaError) as exc_info:
        anyio.run(run)
    assert exc_info.value.code == "VALIDACION"
    assert service._postventa.csat == {}


# -- evidence ownership (B3) and unit-of-work commit (B1) ---------------------------


class _Conversations:
    def __init__(self, owned: dict):
        self.owned = owned  # conversation_id -> customer uuid

    async def find_owned(self, conversation_id, owner):
        return object() if self.owned.get(conversation_id) == owner.customer_id else None


class _EvidenceRows:
    def __init__(self):
        self.rows = []

    async def guardar(self, row):
        self.rows.append(row)
        return row


class _CountingPostventa(FakePostventaPort):
    def __init__(self):
        super().__init__()
        self.uploads = 0

    async def upload_evidence(self, **kwargs):
        self.uploads += 1
        return await super().upload_evidence(**kwargs)


class _Uow:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


def _evidence_service(owned: dict):
    postventa = _CountingPostventa()
    evidence = _EvidenceRows()
    uow = _Uow()
    service = PostventaService(
        postventa=postventa,
        sales=FakeSalesPort(),
        evidence=evidence,
        conversations=_Conversations(owned),
        uow=uow,
    )
    return service, postventa, evidence, uow


def _upload(service, conversation_id, customer_id):
    return service.upload_evidence(
        holder_token=HOLDER,
        file_name="foto.png",
        content_type="image/png",
        content=b"png",
        conversation_id=conversation_id,
        customer_id=customer_id,
    )


def test_evidence_for_own_conversation_is_saved_and_committed():
    conversation_id, customer = uuid.uuid4(), uuid.UUID(CUSTOMER)
    service, postventa, evidence, uow = _evidence_service({conversation_id: customer})

    anyio.run(_upload, service, conversation_id, customer)

    assert postventa.uploads == 1
    assert len(evidence.rows) == 1
    assert uow.commits == 1


@pytest.mark.parametrize("who", ["foreign", "missing", "no_customer"])
def test_evidence_for_foreign_or_missing_conversation_is_rejected_before_g5(who):
    conversation_id, customer = uuid.uuid4(), uuid.UUID(CUSTOMER)
    service, postventa, evidence, uow = _evidence_service(
        {conversation_id: uuid.uuid4()} if who == "foreign" else {}
    )
    caller = None if who == "no_customer" else customer

    with pytest.raises(PostventaError) as exc_info:
        anyio.run(_upload, service, conversation_id, caller)

    assert exc_info.value.code == "RECURSO_NO_ENCONTRADO"
    assert postventa.uploads == 0
    assert evidence.rows == [] and uow.commits == 0


def test_claim_and_return_refs_are_committed():
    service = _service()
    uow = _Uow()
    service._uow = uow
    order = anyio.run(_create_order, service._sales)

    async def run():
        await service.create_claim(
            customer_id=CUSTOMER,
            holder_token=HOLDER,
            payload={
                "pedidoId": order["pedidoId"],
                "tipo": "RECLAMO",
                "motivo": "PRODUCTO_DEFECTUOSO_O_INCORRECTO",
                "detalle": "La suela se despegó al primer uso.",
                "consumidor": {
                    "nombreCompleto": "Ana",
                    "documento": "72458912",
                    "email": "ana@example.com",
                    "telefono": "+51999888777",
                },
            },
            idempotency_key="claim-commit",
        )
        await service.create_return(
            customer_id=CUSTOMER,
            holder_token=HOLDER,
            payload={
                "pedidoId": order["pedidoId"],
                "tipo": "CAMBIO",
                "motivo": "OTRO",
                "items": [{"productoId": "P-1", "cantidad": 1}],
                "evidencias": [],
            },
            idempotency_key="return-commit",
        )

    anyio.run(run)
    assert uow.commits == 2
