"""Tests for PostventaService: ownership, idempotency and normalization."""
import anyio
import pytest

from src.adapters.outbound.fakes.fake_postventa import FakePostventaPort
from src.application.dto.postventa_dto import PostventaError
from src.application.use_cases.postventa_service import PostventaService
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
        "motivo": "PRODUCTO_DEFECTUOSO",
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
        "motivo": "OTRO",
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
