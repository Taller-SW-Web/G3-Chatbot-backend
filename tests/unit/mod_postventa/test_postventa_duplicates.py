"""Duplicate detection for claims and returns, with `forzar` (SPEC-19/21)."""
import anyio
import pytest

from src.modules.mod_postventa.adapters.fake_postventa import FakePostventaPort
from src.modules.mod_postventa.application.postventa_dto import PostventaError
from src.modules.mod_postventa.application.postventa_service import PostventaService
from src.shared.infrastructure.fakes.fake_ventas import FakeSalesPort

CUSTOMER = "11111111-1111-1111-1111-111111111111"
HOLDER = f"fake-access-{CUSTOMER}"


class MemoryRefs:
    def __init__(self):
        self.rows = {}

    async def guardar(self, row):
        self.rows[row.idempotency_key] = row
        return row

    async def obtener_por_idempotency_key(self, key):
        return self.rows.get(key)


def service():
    port = FakePostventaPort()
    sales = FakeSalesPort()
    return PostventaService(
        postventa=port,
        sales=sales,
        claim_refs=MemoryRefs(),
        return_refs=MemoryRefs(),
    ), port, sales


async def create_order(sales):
    return await sales.create_order(
        {
            "canal": "CHATBOT",
            "contacto": {"clienteId": CUSTOMER, "nombreCompleto": "Ana"},
            "items": [{"productoId": "P-1", "sku": "SKU-1", "cantidad": 1, "precioUnitario": 10}],
            "envio": {"distrito": "Miraflores", "costo": 0},
            "pago": {"total": 10},
        },
        idempotency_key="order-key-dups",
    )


def claim_payload(order_id, **extra):
    return {
        "pedidoId": order_id,
        "tipo": "RECLAMO",
        "motivo": "PRODUCTO_DEFECTUOSO",
        "detalle": "La suela se despegó al primer uso.",
        "consumidor": {
            "nombreCompleto": "Ana",
            "documento": "72458912",
            "email": "ana@example.com",
            "telefono": "+51999888777",
        },
        **extra,
    }


def return_payload(order_id, **extra):
    return {
        "pedidoId": order_id,
        "tipo": "DEVOLUCION_DINERO",
        "motivo": "TALLA_INCORRECTA",
        "descripcion": "Me quedó chica.",
        "items": [{"productoId": "P-1", "cantidad": 1}],
        **extra,
    }


def test_open_claim_with_same_order_and_reason_is_409_with_the_existing_code():
    svc, port, sales = service()
    order = anyio.run(create_order, sales)

    async def run():
        await svc.create_claim(
            customer_id=CUSTOMER, holder_token=HOLDER,
            payload=claim_payload(order["pedidoId"]), idempotency_key="claim-dup-1",
        )
        await svc.create_claim(
            customer_id=CUSTOMER, holder_token=HOLDER,
            payload=claim_payload(order["pedidoId"], motivo="PRODUCTO_EQUIVOCADO"),
            idempotency_key="claim-dup-2",
        )
        await svc.create_claim(
            customer_id=CUSTOMER, holder_token=HOLDER,
            payload=claim_payload(order["pedidoId"]), idempotency_key="claim-dup-3",
        )

    with pytest.raises(PostventaError) as exc:
        anyio.run(run)
    assert exc.value.code == "RECLAMO_DUPLICADO"
    assert exc.value.extra["codigoSeguimiento"].startswith("REC-")
    assert len(port.claims) == 1


def test_forzar_true_registers_the_claim_anyway():
    svc, port, sales = service()
    order = anyio.run(create_order, sales)

    async def run():
        await svc.create_claim(
            customer_id=CUSTOMER, holder_token=HOLDER,
            payload=claim_payload(order["pedidoId"]), idempotency_key="claim-force-1",
        )
        return await svc.create_claim(
            customer_id=CUSTOMER, holder_token=HOLDER,
            payload=claim_payload(order["pedidoId"], forzar=True),
            idempotency_key="claim-force-2",
        )

    second = anyio.run(run)
    assert second["codigoSeguimiento"].startswith("REC-")
    assert len(port.claims) == 2


def test_closed_claim_does_not_count_as_duplicate():
    svc, port, sales = service()
    order = anyio.run(create_order, sales)

    async def run():
        first = await svc.create_claim(
            customer_id=CUSTOMER, holder_token=HOLDER,
            payload=claim_payload(order["pedidoId"]), idempotency_key="claim-closed-1",
        )
        port.claims[first["codigoSeguimiento"]]["estado"] = "ATENDIDO"
        return await svc.create_claim(
            customer_id=CUSTOMER, holder_token=HOLDER,
            payload=claim_payload(order["pedidoId"]), idempotency_key="claim-closed-2",
        )

    second = anyio.run(run)
    assert second["codigoSeguimiento"].startswith("REC-")
    assert len(port.claims) == 2


def test_open_return_with_same_order_reason_and_lines_is_409():
    svc, port, sales = service()
    order = anyio.run(create_order, sales)

    async def run():
        await svc.create_return(
            customer_id=CUSTOMER, holder_token=HOLDER,
            payload=return_payload(order["pedidoId"]), idempotency_key="return-dup-1",
        )
        await svc.create_return(
            customer_id=CUSTOMER, holder_token=HOLDER,
            payload=return_payload(order["pedidoId"]), idempotency_key="return-dup-2",
        )

    with pytest.raises(PostventaError) as exc:
        anyio.run(run)
    assert exc.value.code == "DEVOLUCION_DUPLICADA"
    assert exc.value.extra["devolucionId"].startswith("DEV-")
    assert len(port.returns) == 1


def test_return_for_another_line_is_not_a_duplicate():
    svc, port, sales = service()
    order = anyio.run(create_order, sales)

    async def run():
        await svc.create_return(
            customer_id=CUSTOMER, holder_token=HOLDER,
            payload=return_payload(order["pedidoId"]), idempotency_key="return-line-1",
        )
        return await svc.create_return(
            customer_id=CUSTOMER, holder_token=HOLDER,
            payload=return_payload(
                order["pedidoId"],
                items=[{"productoId": "P-2", "cantidad": 1}],
            ),
            idempotency_key="return-line-2",
        )

    second = anyio.run(run)
    assert second["devolucionId"].startswith("DEV-")
    assert len(port.returns) == 2


def test_forzar_true_registers_the_return_anyway():
    svc, port, sales = service()
    order = anyio.run(create_order, sales)

    async def run():
        await svc.create_return(
            customer_id=CUSTOMER, holder_token=HOLDER,
            payload=return_payload(order["pedidoId"]), idempotency_key="return-force-1",
        )
        return await svc.create_return(
            customer_id=CUSTOMER, holder_token=HOLDER,
            payload=return_payload(order["pedidoId"], forzar=True),
            idempotency_key="return-force-2",
        )

    second = anyio.run(run)
    assert second["devolucionId"].startswith("DEV-")
    assert len(port.returns) == 2
