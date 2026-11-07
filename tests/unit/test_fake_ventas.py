"""FakeSalesPort: idempotency, restart-safe ids and per-customer visibility (SALES_MODE=fake)."""
import pytest

from src.adapters.outbound.fakes.fake_ventas import FakeSalesPort
from src.ports.outbound.module_errors import ModuleRequestError


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
