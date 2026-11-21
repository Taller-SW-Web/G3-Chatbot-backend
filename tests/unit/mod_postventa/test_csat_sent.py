"""CSAT marks `order_ref.csat_sent` on success or on Ventas 409 (SPEC-24)."""
import anyio
import pytest

from src.modules.mod_postventa.adapters.fake_postventa import FakePostventaPort
from src.modules.mod_postventa.application.postventa_service import PostventaService
from src.shared.domain.module_errors import ModuleRequestError
from src.shared.infrastructure.fakes.fake_ventas import FakeSalesPort

CUSTOMER = "11111111-1111-1111-1111-111111111111"
HOLDER = f"fake-access-{CUSTOMER}"


class FakeOrderRefs:
    def __init__(self):
        self.marked = []

    async def marcar_csat_enviado(self, order_id):
        self.marked.append(order_id)


def setup():
    sales = FakeSalesPort()

    async def create():
        return await sales.create_order(
            {
                "canal": "CHATBOT",
                "contacto": {"clienteId": CUSTOMER, "nombreCompleto": "Ana"},
                "items": [{"productoId": "P-1", "sku": "SKU-1", "cantidad": 1, "precioUnitario": 10}],
                "envio": {"distrito": "Miraflores", "costo": 0},
                "pago": {"total": 10},
            },
            idempotency_key="order-csat",
        )

    order = anyio.run(create)
    # Mark the fake order as delivered, which CSAT requires.
    sales.orders[order["pedidoId"]]["estado"] = "ENTREGADO"
    refs = FakeOrderRefs()
    service = PostventaService(
        postventa=FakePostventaPort(), sales=sales, order_refs=refs,
    )
    return service, order["pedidoId"], refs


def test_successful_csat_marks_the_local_order():
    service, order_id, refs = setup()

    async def run():
        await service.create_csat(
            customer_id=CUSTOMER,
            holder_token=HOLDER,
            order_id=order_id,
            payload={"puntuacion": 5, "comentario": "Excelente"},
        )

    anyio.run(run)
    assert refs.marked == [order_id]


def test_ventas_409_also_marks_the_local_order():
    service, order_id, refs = setup()

    async def duplicate(payload, *, holder_token, correlation_id=None):
        raise ModuleRequestError("postventa", 409, "CSAT_ALREADY_SUBMITTED")

    service._postventa.create_csat = duplicate

    async def run():
        await service.create_csat(
            customer_id=CUSTOMER,
            holder_token=HOLDER,
            order_id=order_id,
            payload={"puntuacion": 3},
        )

    with pytest.raises(ModuleRequestError):
        anyio.run(run)
    assert refs.marked == [order_id]
