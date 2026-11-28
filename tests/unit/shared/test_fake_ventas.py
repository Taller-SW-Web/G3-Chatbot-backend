"""FakeSalesPort: idempotency, restart-safe ids and per-customer visibility (SALES_MODE=fake)."""
import pytest

from src.shared.domain.module_errors import ModuleRequestError
from src.shared.infrastructure.fakes.fake_ventas import FakeSalesPort


def snapshot(customer_id: str, total: float = 100.0) -> dict:
    return {
        "canal": "CHATBOT",
        "contacto": {"clienteId": customer_id},
        "items": [{"sku": "SKU-1", "cantidad": 1}],
        "envio": {},
        "pago": {"total": total},
    }


def token(customer_id: str) -> str:
    return f"fake-access-{customer_id}"


@pytest.mark.asyncio
async def test_same_idempotency_key_returns_the_same_order():
    sales = FakeSalesPort()
    first = await sales.create_order(snapshot("c1"), idempotency_key="k1")
    second = await sales.create_order(snapshot("c1"), idempotency_key="k1")
    assert first == second
    assert len(sales.orders) == 1
    assert len(sales.created_calls) == 1


@pytest.mark.asyncio
async def test_different_keys_create_different_orders():
    sales = FakeSalesPort()
    first = await sales.create_order(snapshot("c1"), idempotency_key="k1")
    second = await sales.create_order(snapshot("c1"), idempotency_key="k2")
    assert first["pedidoId"] != second["pedidoId"]


@pytest.mark.asyncio
async def test_order_ids_do_not_collide_across_restarts():
    ids = set()
    for _ in range(25):  # a new instance is a process restart
        created = await FakeSalesPort().create_order(snapshot("c1"), idempotency_key="k1")
        ids.add(created["pedidoId"])
    assert len(ids) == 25
    assert all(order_id.startswith("PED-") for order_id in ids)


@pytest.mark.asyncio
async def test_order_ids_carry_enough_randomness_for_a_load_test():
    # 6 hex chars (16.7 M values) collided in a k6 run of ~1,500 orders (order_ref PK);
    # 10 hex chars keep the birthday probability negligible at that volume.
    created = await FakeSalesPort().create_order(snapshot("c1"), idempotency_key="k1")
    random_part = created["pedidoId"].rsplit("-", 1)[1]
    assert len(random_part) == 10
    int(random_part, 16)


@pytest.mark.asyncio
async def test_each_customer_lists_only_their_orders():
    sales = FakeSalesPort()
    mine = await sales.create_order(snapshot("c1"), idempotency_key="k1")
    await sales.create_order(snapshot("c2"), idempotency_key="k2")

    listing = await sales.list_customer_orders(token("c1"))
    assert [item["pedidoId"] for item in listing["items"]] == [mine["pedidoId"]]
    assert listing["total"] == 1
    assert (await sales.list_customer_orders(token("c3")))["items"] == []


@pytest.mark.asyncio
async def test_detail_of_a_foreign_order_looks_like_not_found():
    sales = FakeSalesPort()
    created = await sales.create_order(snapshot("c1"), idempotency_key="k1")

    detail = await sales.get_customer_order(created["pedidoId"], token("c1"))
    assert detail["pedidoId"] == created["pedidoId"]
    with pytest.raises(ModuleRequestError) as exc:
        await sales.get_customer_order(created["pedidoId"], token("c2"))
    assert exc.value.status == 404


@pytest.mark.asyncio
async def test_detail_follows_the_g5_shape_with_payment_and_history():
    sales = FakeSalesPort()
    created = await sales.create_order(snapshot("c1", total=80.0), idempotency_key="k1")

    detail = await sales.get_customer_order(created["pedidoId"], token("c1"))
    assert detail["pago"]["total"] == 80.0
    assert detail["pago"]["estado"] == "PENDIENTE"
    assert [entry["estado"] for entry in detail["historialEstados"]] == ["CREADO"]


@pytest.mark.asyncio
async def test_stock_cancelled_order_records_reason_and_actor():
    sales = FakeSalesPort()
    created = await sales.create_order(snapshot("c1"), idempotency_key="k1")
    sales.cancel_for_stock(created["pedidoId"])

    detail = await sales.get_customer_order(created["pedidoId"], token("c1"))
    assert detail["estado"] == "ANULADO"
    assert detail["pago"]["estado"] == "CONFIRMADO"
    last = detail["historialEstados"][-1]
    assert (last["estado"], last["motivo"], last["actor"]) == (
        "ANULADO", "STOCK_NO_DISPONIBLE", "SISTEMA_PRODUCTOS",
    )


@pytest.mark.asyncio
async def test_payment_on_a_cancelled_order_answers_anulado_and_keeps_it_cancelled():
    sales = FakeSalesPort()
    created = await sales.create_order(snapshot("c1"), idempotency_key="k1")
    order_id = created["pedidoId"]
    sales.cancel_for_stock(order_id, paid=False)

    result = await sales.notify_payment(order_id, {"transaccionId": "TX-1"})

    assert result["nuevoEstado"] == "ANULADO"
    assert result["transaccionId"] == "TX-1"
    detail = await sales.get_customer_order(order_id, token("c1"))
    assert detail["estado"] == "ANULADO"
    assert detail["pago"]["estado"] == "CONFIRMADO"


@pytest.mark.asyncio
async def test_payment_on_an_open_order_moves_it_to_pagado():
    sales = FakeSalesPort()
    created = await sales.create_order(snapshot("c1"), idempotency_key="k1")

    result = await sales.notify_payment(created["pedidoId"], {"transaccionId": "TX-1"})

    assert result["nuevoEstado"] == "PAGADO"
    detail = await sales.get_customer_order(created["pedidoId"], token("c1"))
    assert [entry["estado"] for entry in detail["historialEstados"]] == ["CREADO", "PAGADO"]


@pytest.mark.asyncio
async def test_cancelling_an_already_cancelled_order_raises_the_g5_409():
    sales = FakeSalesPort()
    created = await sales.create_order(snapshot("c1"), idempotency_key="k1")
    sales.cancel_for_stock(created["pedidoId"])

    with pytest.raises(ModuleRequestError) as exc:
        await sales.cancel_order(created["pedidoId"])
    assert (exc.value.status, exc.value.code) == (409, "PEDIDO_NO_ANULABLE")


@pytest.mark.asyncio
async def test_cancelling_an_open_order_still_succeeds():
    sales = FakeSalesPort()
    created = await sales.create_order(snapshot("c1"), idempotency_key="k1")

    result = await sales.cancel_order(created["pedidoId"])

    assert result["estadoPedido"] == "ANULADO"
    detail = await sales.get_customer_order(created["pedidoId"], token("c1"))
    assert detail["historialEstados"][-1]["motivo"] == "PAGO_NO_COMPLETADO"


@pytest.mark.asyncio
async def test_service_read_returns_the_order_without_a_holder_token():
    sales = FakeSalesPort()
    created = await sales.create_order(snapshot("c1"), idempotency_key="k1")
    sales.cancel_for_stock(created["pedidoId"])

    order = await sales.get_order_as_service(created["pedidoId"])

    assert order["estado"] == "ANULADO"
    assert order["historialEstados"][-1]["estado"] == "ANULADO"


@pytest.mark.asyncio
async def test_service_read_of_an_unknown_order_is_a_404():
    with pytest.raises(ModuleRequestError) as exc:
        await FakeSalesPort().get_order_as_service("PED-NOPE")
    assert (exc.value.status, exc.value.code) == (404, "PEDIDO_NO_ENCONTRADO")
