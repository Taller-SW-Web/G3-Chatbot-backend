"""Unit tests for anonymous cart merge on login (SPEC-03 Req. 4, #184)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.modules.mod_cart.application.manage_cart_use_case import ManageCartUseCase
from src.shared.application.ports.cart_merger_port import CART_MERGED_MESSAGE


@dataclass
class _FakeCart:
    id: uuid.UUID
    customer_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    status: str = "ACTIVE"
    anonymous_sid: str | None = None
    totals_snapshot: dict | None = None


@dataclass
class _FakeItem:
    id: uuid.UUID
    cart_id: uuid.UUID
    product_id: str
    sku: str
    name: str
    variant_description: str | None
    image_url: str | None
    quantity: int
    unit_price_ref: float


class _InMemoryCarts:
    def __init__(self) -> None:
        self.carts: dict[uuid.UUID, _FakeCart] = {}

    def add_cart(self, cart: _FakeCart) -> _FakeCart:
        self.carts[cart.id] = cart
        return cart

    async def obtener_abierto_por_cliente(self, customer_id: uuid.UUID):
        for c in self.carts.values():
            if c.customer_id == customer_id and c.status in ("ACTIVE", "IN_CHECKOUT"):
                return c
        return None

    async def listar_abiertos_por_sid(self, anonymous_sid: str):
        return [
            c
            for c in self.carts.values()
            if c.anonymous_sid == anonymous_sid
            and c.customer_id is None
            and c.status in ("ACTIVE", "IN_CHECKOUT")
        ]

    async def crear_para_cliente(self, customer_id: uuid.UUID):
        cart = _FakeCart(id=uuid.uuid4(), customer_id=customer_id)
        self.carts[cart.id] = cart
        return cart

    async def cambiar_estado(self, cart_id: uuid.UUID, estado: str):
        self.carts[cart_id].status = estado
        return self.carts[cart_id]

    async def set_totals(self, cart_id: uuid.UUID, snapshot):
        self.carts[cart_id].totals_snapshot = snapshot


class _InMemoryItems:
    def __init__(self) -> None:
        self.items: dict[uuid.UUID, _FakeItem] = {}

    def seed(self, cart_id: uuid.UUID, sku: str, quantity: int, name: str = "Producto") -> _FakeItem:
        item = _FakeItem(
            id=uuid.uuid4(),
            cart_id=cart_id,
            product_id=f"prod-{sku}",
            sku=sku,
            name=name,
            variant_description=None,
            image_url=None,
            quantity=quantity,
            unit_price_ref=99.0,
        )
        self.items[item.id] = item
        return item

    async def listar_por_cart(self, cart_id: uuid.UUID):
        return [i for i in self.items.values() if i.cart_id == cart_id]

    async def agregar(
        self,
        *,
        cart_id,
        product_id,
        sku,
        name,
        variant_description,
        image_url,
        quantity,
        unit_price_ref,
    ):
        item = _FakeItem(
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
        self.items[item.id] = item
        return item

    async def set_quantity(self, item_id, *, quantity: int, unit_price_ref: float):
        self.items[item_id].quantity = quantity
        self.items[item_id].unit_price_ref = unit_price_ref

    async def eliminar(self, item_id) -> bool:
        return self.items.pop(item_id, None) is not None


class _FakeInventory:
    def __init__(self, stock_by_sku: dict[str, int] | None = None, *, fail: bool = False):
        self._stock = stock_by_sku or {}
        self._fail = fail

    async def get_availability(self, skus: list[str]):
        if self._fail:
            raise RuntimeError("inventory down")
        rows = []
        for sku in skus:
            qty = self._stock.get(sku, 50)
            rows.append(
                {
                    "sku": sku,
                    "estado": "AGOTADO" if qty == 0 else "DISPONIBLE",
                    "disponible": qty,
                }
            )
        return rows

    async def verify_quantities(self, lines: list[dict]):
        if self._fail:
            raise RuntimeError("inventory down")
        verdicts = []
        for line in lines:
            sku = line["sku"]
            qty = self._stock.get(sku, 50)
            verdicts.append(
                {
                    "sku": sku,
                    "result": "OK" if line["quantity"] <= qty else "STOCK_INSUFICIENTE",
                    "disponible": qty,
                }
            )
        return verdicts


def _make_use_case(
    carts: _InMemoryCarts,
    items: _InMemoryItems,
    inventory: _FakeInventory | None = None,
) -> tuple[ManageCartUseCase, SimpleNamespace]:
    uow = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    uc = ManageCartUseCase(
        carts=carts,
        items=items,
        catalog=AsyncMock(),
        inventory=inventory or _FakeInventory(),
        conversation_context=AsyncMock(),
        uow=uow,
    )
    return uc, uow


@pytest.mark.asyncio
async def test_merge_combines_duplicate_skus_adds_new_and_marks_merged_once() -> None:
    carts = _InMemoryCarts()
    items = _InMemoryItems()
    customer_id = uuid.uuid4()

    anon_cart = carts.add_cart(
        _FakeCart(id=uuid.uuid4(), conversation_id=uuid.uuid4(), anonymous_sid="sid-1")
    )
    items.seed(anon_cart.id, "SKU-A", 2, name="Zapatilla A")
    items.seed(anon_cart.id, "SKU-C", 1, name="Polo C")

    cust_cart = carts.add_cart(_FakeCart(id=uuid.uuid4(), customer_id=customer_id))
    items.seed(cust_cart.id, "SKU-A", 1, name="Zapatilla A")
    items.seed(cust_cart.id, "SKU-B", 1, name="Short B")

    uc, uow = _make_use_case(carts, items)
    result = await uc.merge_anonymous_cart(anonymous_sid="sid-1", customer_id=customer_id)

    assert result.merged is True
    assert result.message == CART_MERGED_MESSAGE
    assert result.adjustments == []
    assert anon_cart.status == "MERGED"
    uow.commit.assert_awaited_once()

    by_sku = {i.sku: i.quantity for i in await items.listar_por_cart(cust_cart.id)}
    assert by_sku == {"SKU-A": 3, "SKU-B": 1, "SKU-C": 1}

    # Idempotence: a second login with the same sid never merges twice.
    second = await uc.merge_anonymous_cart(anonymous_sid="sid-1", customer_id=customer_id)
    assert second.merged is False
    by_sku_after = {i.sku: i.quantity for i in await items.listar_por_cart(cust_cart.id)}
    assert by_sku_after == {"SKU-A": 3, "SKU-B": 1, "SKU-C": 1}


@pytest.mark.asyncio
async def test_merge_without_anonymous_cart_is_noop() -> None:
    carts = _InMemoryCarts()
    items = _InMemoryItems()
    uc, uow = _make_use_case(carts, items)

    result = await uc.merge_anonymous_cart(anonymous_sid="sid-none", customer_id=uuid.uuid4())

    assert result.merged is False
    assert result.message is None
    uow.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_merge_caps_quantity_at_10_and_reports_adjustment() -> None:
    carts = _InMemoryCarts()
    items = _InMemoryItems()
    customer_id = uuid.uuid4()

    anon_cart = carts.add_cart(
        _FakeCart(id=uuid.uuid4(), conversation_id=uuid.uuid4(), anonymous_sid="sid-cap")
    )
    items.seed(anon_cart.id, "SKU-A", 6, name="Zapatilla A")

    cust_cart = carts.add_cart(_FakeCart(id=uuid.uuid4(), customer_id=customer_id))
    items.seed(cust_cart.id, "SKU-A", 7, name="Zapatilla A")

    uc, _ = _make_use_case(carts, items)
    result = await uc.merge_anonymous_cart(anonymous_sid="sid-cap", customer_id=customer_id)

    assert result.merged is True
    by_sku = {i.sku: i.quantity for i in await items.listar_por_cart(cust_cart.id)}
    assert by_sku["SKU-A"] == 10
    assert result.adjustments == [
        {
            "sku": "SKU-A",
            "nombre": "Zapatilla A",
            "cantidadSolicitada": 13,
            "cantidadFinal": 10,
            "motivo": "LIMITE_CANTIDAD",
        }
    ]


@pytest.mark.asyncio
async def test_merge_enforces_20_line_limit() -> None:
    carts = _InMemoryCarts()
    items = _InMemoryItems()
    customer_id = uuid.uuid4()

    cust_cart = carts.add_cart(_FakeCart(id=uuid.uuid4(), customer_id=customer_id))
    for idx in range(20):
        items.seed(cust_cart.id, f"SKU-{idx:02d}", 1, name=f"Prod {idx}")

    anon_cart = carts.add_cart(
        _FakeCart(id=uuid.uuid4(), conversation_id=uuid.uuid4(), anonymous_sid="sid-20")
    )
    items.seed(anon_cart.id, "SKU-EXTRA", 2, name="Extra")

    uc, _ = _make_use_case(carts, items)
    result = await uc.merge_anonymous_cart(anonymous_sid="sid-20", customer_id=customer_id)

    assert result.merged is True
    assert len(await items.listar_por_cart(cust_cart.id)) == 20
    assert result.adjustments == [
        {
            "sku": "SKU-EXTRA",
            "nombre": "Extra",
            "cantidadSolicitada": 2,
            "cantidadFinal": 0,
            "motivo": "LIMITE_LINEAS",
        }
    ]


@pytest.mark.asyncio
async def test_merge_adjusts_to_available_stock_and_reports_per_line() -> None:
    carts = _InMemoryCarts()
    items = _InMemoryItems()
    customer_id = uuid.uuid4()

    anon_cart = carts.add_cart(
        _FakeCart(id=uuid.uuid4(), conversation_id=uuid.uuid4(), anonymous_sid="sid-stock")
    )
    items.seed(anon_cart.id, "SKU-LOW", 4, name="Zapatilla Stock Bajo")
    items.seed(anon_cart.id, "SKU-ZERO", 2, name="Zapatilla Agotada")

    cust_cart = carts.add_cart(_FakeCart(id=uuid.uuid4(), customer_id=customer_id))
    items.seed(cust_cart.id, "SKU-LOW", 2, name="Zapatilla Stock Bajo")

    inventory = _FakeInventory({"SKU-LOW": 3, "SKU-ZERO": 0})
    uc, _ = _make_use_case(carts, items, inventory=inventory)
    result = await uc.merge_anonymous_cart(anonymous_sid="sid-stock", customer_id=customer_id)

    assert result.merged is True
    by_sku = {i.sku: i.quantity for i in await items.listar_por_cart(cust_cart.id)}
    assert by_sku == {"SKU-LOW": 3}
    assert len(result.adjustments) == 2
    assert {a["sku"]: (a["cantidadFinal"], a["motivo"]) for a in result.adjustments} == {
        "SKU-LOW": (3, "STOCK_INSUFICIENTE"),
        "SKU-ZERO": (0, "STOCK_INSUFICIENTE"),
    }


@pytest.mark.asyncio
async def test_merge_degrades_safely_when_inventory_is_unavailable() -> None:
    carts = _InMemoryCarts()
    items = _InMemoryItems()
    customer_id = uuid.uuid4()

    anon_cart = carts.add_cart(
        _FakeCart(id=uuid.uuid4(), conversation_id=uuid.uuid4(), anonymous_sid="sid-down")
    )
    items.seed(anon_cart.id, "SKU-A", 3, name="Zapatilla A")

    uc, _ = _make_use_case(carts, items, inventory=_FakeInventory(fail=True))
    result = await uc.merge_anonymous_cart(anonymous_sid="sid-down", customer_id=customer_id)

    assert result.merged is True
    assert anon_cart.status == "MERGED"
    cust_cart = await carts.obtener_abierto_por_cliente(customer_id)
    assert cust_cart is not None
    by_sku = {i.sku: i.quantity for i in await items.listar_por_cart(cust_cart.id)}
    assert by_sku == {"SKU-A": 3}
