"""Duplicate detection for claims and returns, with `forzar` (SPEC-19/21)."""
import logging

import anyio
import pytest

from src.modules.mod_postventa.adapters.fake_postventa import FakePostventaPort
from src.modules.mod_postventa.application.postventa_dto import PostventaError
from src.modules.mod_postventa.application.postventa_service import PostventaService
from src.shared.domain.request_context import correlation_id_var
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
        "motivo": "PRODUCTO_DEFECTUOSO_O_INCORRECTO",
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
            payload=claim_payload(order["pedidoId"], motivo="PRODUCTO_DEFECTUOSO_O_INCORRECTO"),
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


# -- fail-open and full pagination (issue #251) --------------------------------------


class _FailingListPort(FakePostventaPort):
    async def list_customer_returns(self, *args, **kwargs):
        raise TimeoutError("G5 listado de devoluciones caído")

    async def list_customer_claims(self, *args, **kwargs):
        raise TimeoutError("G5 listado de reclamos caído")


def test_return_listing_failure_is_fail_open_with_the_request_correlation_id(caplog):
    port = _FailingListPort()
    svc = PostventaService(postventa=port, sales=FakeSalesPort(), return_refs=MemoryRefs())
    token = correlation_id_var.set("corr-251-return")
    try:
        with caplog.at_level(
            logging.WARNING,
            logger="src.modules.mod_postventa.application.postventa_service",
        ):
            detail = anyio.run(
                lambda: svc.create_return(
                    customer_id=CUSTOMER,
                    holder_token=HOLDER,
                    payload=return_payload("PED-2026-00001"),
                    idempotency_key="return-fail-open",
                )
            )
    finally:
        correlation_id_var.reset(token)

    assert detail["devolucionId"].startswith("DEV-")
    assert len(port.returns) == 1
    assert "corr-251-return" in caplog.text
    assert "fail-open" in caplog.text


def test_claim_listing_failure_is_fail_open_with_correlation_id(caplog):
    port = _FailingListPort()
    sales = FakeSalesPort()
    svc = PostventaService(postventa=port, sales=sales, claim_refs=MemoryRefs())
    order = anyio.run(create_order, sales)

    with caplog.at_level(
        logging.WARNING,
        logger="src.modules.mod_postventa.application.postventa_service",
    ):
        detail = anyio.run(
            lambda: svc.create_claim(
                customer_id=CUSTOMER,
                holder_token=HOLDER,
                payload=claim_payload(order["pedidoId"]),
                idempotency_key="claim-fail-open",
                correlation_id="corr-251-claim",
            )
        )

    assert detail["codigoSeguimiento"].startswith("REC-")
    assert len(port.claims) == 1
    assert "corr-251-claim" in caplog.text
    assert "fail-open" in caplog.text


def test_duplicate_return_in_position_51_is_detected():
    port = FakePostventaPort()
    svc = PostventaService(postventa=port, sales=FakeSalesPort(), return_refs=MemoryRefs())

    async def seed():
        for index in range(50):
            await port.create_return(
                {
                    "pedidoId": f"PED-OTHER-{index}",
                    "tipo": "CAMBIO",
                    "motivo": "OTRO",
                    "items": [{"productoId": "P-1", "cantidad": 1}],
                },
                holder_token=HOLDER,
            )
        target = await port.create_return(
            {
                "pedidoId": "PED-2026-00051",
                "tipo": "DEVOLUCION_DINERO",
                "motivo": "OTRO",
                "items": [{"productoId": "P-1", "cantidad": 1}],
            },
            holder_token=HOLDER,
        )
        return target["devolucionId"]

    target_id = anyio.run(seed)

    with pytest.raises(PostventaError) as exc:
        anyio.run(
            lambda: svc.create_return(
                customer_id=CUSTOMER,
                holder_token=HOLDER,
                payload=return_payload("PED-2026-00051"),
                idempotency_key="return-page-51",
            )
        )
    assert exc.value.code == "DEVOLUCION_DUPLICADA"
    assert exc.value.extra["devolucionId"] == target_id
    assert len(port.returns) == 51


def test_duplicate_claim_in_position_51_is_detected():
    port = FakePostventaPort()
    sales = FakeSalesPort()
    svc = PostventaService(postventa=port, sales=sales, claim_refs=MemoryRefs())
    order = anyio.run(create_order, sales)

    async def seed():
        for index in range(50):
            await port.create_claim(
                {
                    "pedidoId": f"PED-OTHER-{index}",
                    "tipo": "RECLAMO",
                    "motivo": "PRODUCTO_DEFECTUOSO_O_INCORRECTO",
                    "detalle": "La suela se despegó al primer uso.",
                },
                holder_token=HOLDER,
            )
        return await port.create_claim(
            {
                "pedidoId": order["pedidoId"],
                "tipo": "RECLAMO",
                "motivo": "PRODUCTO_DEFECTUOSO_O_INCORRECTO",
                "detalle": "La suela se despegó al primer uso.",
            },
            holder_token=HOLDER,
        )

    target = anyio.run(seed)

    with pytest.raises(PostventaError) as exc:
        anyio.run(
            lambda: svc.create_claim(
                customer_id=CUSTOMER,
                holder_token=HOLDER,
                payload=claim_payload(order["pedidoId"]),
                idempotency_key="claim-page-51",
            )
        )
    assert exc.value.code == "RECLAMO_DUPLICADO"
    assert exc.value.extra["codigoSeguimiento"] == target["codigoSeguimiento"]
    assert len(port.claims) == 51
