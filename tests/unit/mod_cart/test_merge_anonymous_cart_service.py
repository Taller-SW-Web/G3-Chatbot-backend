"""Unit tests for MergeAnonymousCartService (SPEC-03 Req. 4, #184)."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from src.modules.mod_cart.application.merge_anonymous_cart_service import (
    CART_MERGED_MESSAGE,
    MAX_LINES,
    MAX_QTY_PER_LINE,
    MergeAnonymousCartService,
)


class _FakeCarts:
    def __init__(self, *, anon_carts=None, customer_cart=None) -> None:
        self.anon_carts = list(anon_carts or [])
        self.customer_cart = customer_cart
        self.status_updates: list[tuple[uuid.UUID, str]] = []
        self.totals_updates: list[tuple[uuid.UUID, object]] = []

    async def obtener_abiertos_por_anonymous_sid(self, anonymous_sid: str):
        return [c for c in self.anon_carts if c.status in ("ACTIVE", "IN_CHECKOUT")]

    async def obtener_abierto_por_cliente(self, customer_id: uuid.UUID):
        return self.customer_cart

    async def crear_para_cliente(self, customer_id: uuid.UUID):
        self.customer_cart = SimpleNamespace(
            id=uuid.uuid4(), customer_id=customer_id, status="ACTIVE"
        )
        return self.customer_cart

    async def cambiar_estado(self, cart_id: uuid.UUID, status: str):
        self.status_updates.append((cart_id, status))
        for c in self.anon_carts:
            if c.id == cart_id:
                c.status = status
                return c
        return None

    async def set_totals(self, cart_id: uuid.UUID, snapshot):
        self.totals_updates.append((cart_id, snapshot))


class _FakeItems:
    def __init__(self, by_cart: dict[uuid.UUID, list]) -> None:
        self.by_cart = {k: list(v) for k, v in by_cart.items()}

    async def listar_por_cart(self, cart_id: uuid.UUID):
        return list(self.by_cart.get(cart_id, []))

    async def agregar(
        self,
        *,
        cart_id: uuid.UUID,
        product_id: str,
        sku: str,
        name: str,
        variant_description: str | None,
        image_url: str | None,
        quantity: int,
        unit_price_ref: float,
    ):
        item = SimpleNamespace(
            id=uuid.uuid4(),
            cart_id=cart_id,
            product_id=product_id,
            sku=sku,
            name=name,
            variant_description=variant_description,
            image_url=image_url,
            quantity=quantity,
            unit_price_ref=unit_price_ref,
        )
        self.by_cart.setdefault(cart_id, []).append(item)
        return item

    async def set_quantity(self, item_id: uuid.UUID, *, quantity: int, unit_price_ref: float):
        for rows in self.by_cart.values():
            for row in rows:
                if row.id == item_id:
                    row.quantity = quantity
                    row.unit_price_ref = unit_price_ref


class _FakeInventory:
    def __init__(self, stock_by_sku: dict[str, int | str]) -> None:
        self._stock = dict(stock_by_sku)

    async def verify_quantities(self, lines, *, channel_id="CHATBOT", correlation_id=None):
        results = []
        for line in lines:
            sku = line["sku"]
            qty = int(line["quantity"])
            spec = self._stock.get(sku, 100)
            if spec == "INDETERMINADO":
                verdict = "INDETERMINADO"
            else:
                verdict = "SUFICIENTE" if qty <= int(spec) else "INSUFICIENTE"
            results.append({"sku": sku, "requested_quantity": qty, "result": verdict})
        return results


def _item(cart_id: uuid.UUID, sku: str, qty: int, price: float = 100.0):
    return SimpleNamespace(
        id=uuid.uuid4(),
        cart_id=cart_id,
        product_id=f"prod-{sku}",
        sku=sku,
        name=f"Producto {sku}",
        variant_description="M / Negro",
        image_url=None,
        quantity=qty,
        unit_price_ref=price,
    )


@pytest.mark.asyncio
async def test_no_anonymous_cart_returns_not_merged() -> None:
    svc = MergeAnonymousCartService(_FakeCarts(), _FakeItems({}))
    res = await svc.merge_for_login("hashed-sid", uuid.uuid4())
    assert res.merged is False
    assert res.message is None


@pytest.mark.asyncio
async def test_merges_overlapping_and_new_skus_and_marks_anonymous_merged_once() -> None:
    anon_id = uuid.uuid4()
    cust_cart_id = uuid.uuid4()
    carts = _FakeCarts(
        anon_carts=[SimpleNamespace(id=anon_id, status="ACTIVE")],
        customer_cart=SimpleNamespace(id=cust_cart_id, status="ACTIVE"),
    )
    items = _FakeItems(
        {
            anon_id: [_item(anon_id, "SKU-A", 2), _item(anon_id, "SKU-C", 1)],
            cust_cart_id: [_item(cust_cart_id, "SKU-A", 1), _item(cust_cart_id, "SKU-B", 1)],
        }
    )
    svc = MergeAnonymousCartService(carts, items, _FakeInventory({"SKU-A": 10, "SKU-C": 10}))

    first = await svc.merge_for_login("hashed-sid", uuid.uuid4())
    assert first.merged is True
    assert first.message == CART_MERGED_MESSAGE
    assert first.adjustments == ()
    assert (anon_id, "MERGED") in carts.status_updates
    assert (cust_cart_id, None) in carts.totals_updates

    merged_by_sku = {i.sku: i.quantity for i in items.by_cart[cust_cart_id]}
    assert merged_by_sku == {"SKU-A": 3, "SKU-B": 1, "SKU-C": 1}

    # Second call is a no-op because the anonymous cart is already MERGED.
    second = await svc.merge_for_login("hashed-sid", uuid.uuid4())
    assert second.merged is False


@pytest.mark.asyncio
async def test_clamps_to_live_stock_via_verify_quantities_and_respects_indeterminado() -> None:
    anon_id = uuid.uuid4()
    cust_cart_id = uuid.uuid4()
    carts = _FakeCarts(
        anon_carts=[SimpleNamespace(id=anon_id, status="ACTIVE")],
        customer_cart=SimpleNamespace(id=cust_cart_id, status="ACTIVE"),
    )
    items = _FakeItems(
        {
            anon_id: [
                _item(anon_id, "SKU-LOW", 5),
                _item(anon_id, "SKU-IND", 3),
                _item(anon_id, "SKU-ZERO", 2),
            ],
            cust_cart_id: [_item(cust_cart_id, "SKU-LOW", 2)],
        }
    )
    inv = _FakeInventory({"SKU-LOW": 4, "SKU-IND": "INDETERMINADO", "SKU-ZERO": 0})
    svc = MergeAnonymousCartService(carts, items, inv)

    res = await svc.merge_for_login("hashed-sid", uuid.uuid4())
    assert res.merged is True
    merged_by_sku = {i.sku: i.quantity for i in items.by_cart[cust_cart_id]}
    assert merged_by_sku == {"SKU-LOW": 4, "SKU-IND": 3}
    reasons = {adj["sku"]: (adj["ajustado"], adj["motivo"]) for adj in res.adjustments}
    assert reasons["SKU-LOW"] == (4, "STOCK_INSUFICIENTE")
    assert reasons["SKU-ZERO"] == (0, "SIN_STOCK")


@pytest.mark.asyncio
async def test_enforces_per_line_cap_and_max_lines_cap() -> None:
    anon_id = uuid.uuid4()
    cust_cart_id = uuid.uuid4()
    carts = _FakeCarts(
        anon_carts=[SimpleNamespace(id=anon_id, status="ACTIVE")],
        customer_cart=SimpleNamespace(id=cust_cart_id, status="ACTIVE"),
    )
    existing = [_item(cust_cart_id, f"SKU-{i}", 1) for i in range(MAX_LINES)]
    existing[0].quantity = 8
    items = _FakeItems(
        {
            anon_id: [_item(anon_id, "SKU-0", 5), _item(anon_id, "SKU-OVERFLOW", 1)],
            cust_cart_id: existing,
        }
    )
    svc = MergeAnonymousCartService(carts, items, _FakeInventory({}))

    res = await svc.merge_for_login("hashed-sid", uuid.uuid4())
    assert res.merged is True
    merged_by_sku = {i.sku: i.quantity for i in items.by_cart[cust_cart_id]}
    assert merged_by_sku["SKU-0"] == MAX_QTY_PER_LINE
    assert "SKU-OVERFLOW" not in merged_by_sku
    assert len(merged_by_sku) == MAX_LINES
    motivos = {adj["sku"]: adj["motivo"] for adj in res.adjustments}
    assert motivos["SKU-0"] == "TOPE_POR_LINEA"
    assert motivos["SKU-OVERFLOW"] == "LIMITE_LINEAS"
