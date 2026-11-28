"""Anonymous cart merge on login/MFA (SPEC-03 Req. 4, #184).

`mod-auth` resolves the optional `CART_MERGE` capability and must run it
**before** `link_anonymous_to_customer`, because linking clears the
`anonymous_sid` the cart query depends on. The merge rules (duplicate SKUs,
10-per-line, 20 lines, live stock) are owned by `mod-cart`; these tests wire
its real `MergeAnonymousCartService` to prove the login flow honors them.
"""
from __future__ import annotations

import uuid

import pytest

from src.modules.mod_auth.application.manage_session_use_case import ManageSessionUseCase
from src.modules.mod_cart.application.merge_anonymous_cart_service import (
    CART_MERGED_MESSAGE,
    MAX_LINES,
    MAX_QTY_PER_LINE,
    MergeAnonymousCartService,
)
from src.shared.application.ports.cart_merge_port import CartMergeResult

CUSTOMER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
HASHED_SID = "b6f1c5a3e2d4f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f"


class _FakeSecurity:
    def __init__(self, answer: dict | Exception) -> None:
        self._answer = answer
        self.calls: list[tuple] = []

    async def login(self, email: str, password: str) -> dict:
        self.calls.append(("login", email))
        if isinstance(self._answer, Exception):
            raise self._answer
        return dict(self._answer)

    async def verify_mfa(self, challenge: str, code: str) -> dict:
        self.calls.append(("verify_mfa", challenge, code))
        if isinstance(self._answer, Exception):
            raise self._answer
        return dict(self._answer)

    async def logout(self, refresh_token: str | None) -> None:
        self.calls.append(("logout", refresh_token))


class _FakeValidator:
    async def validate_customer(self, token: str) -> dict:
        return {"sub": str(CUSTOMER_ID), "roles": ["CLIENTE"]}


class _RecordingConversations:
    def __init__(self, events: list[str], pending: str | None = "INICIAR_CHECKOUT") -> None:
        self.events = events
        self.pending = pending
        self.calls: list[tuple[str, uuid.UUID]] = []

    async def link_anonymous_to_customer(self, anonymous_sid: str, customer_id: uuid.UUID):
        self.events.append("link")
        self.calls.append((anonymous_sid, customer_id))
        action, self.pending = self.pending, None
        return (1, action)


class _RecordingCartMerge:
    def __init__(self, events: list[str], result: CartMergeResult | Exception) -> None:
        self.events = events
        self.result = result
        self.calls: list[tuple[str, uuid.UUID]] = []

    async def merge_for_login(self, anonymous_sid: str, customer_id: uuid.UUID) -> CartMergeResult:
        self.events.append("merge")
        self.calls.append((anonymous_sid, customer_id))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _FakeUow:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1
        self.events.append("commit")

    async def rollback(self) -> None:
        self.rollbacks += 1
        self.events.append("rollback")


def _answer() -> dict:
    return {
        "accessToken": "acc-token",
        "refreshToken": "ref-token",
        "expiresIn": 900,
        "usuario": {"id": str(CUSTOMER_ID)},
    }


def _sessions(
    *,
    merge: _RecordingCartMerge | None = None,
    conversations: _RecordingConversations | None = None,
    uow: _FakeUow | None = None,
    security_answer: dict | None = None,
) -> ManageSessionUseCase:
    return ManageSessionUseCase(
        security=_FakeSecurity(security_answer or _answer()),
        validator=_FakeValidator(),
        conversations=conversations,
        cart_merge=merge,
        uow=uow,
    )


# --- capability wiring and response mapping ---------------------------------


@pytest.mark.asyncio
async def test_login_without_anonymous_sid_never_calls_cart_merge() -> None:
    """No `chat_sid` means no anonymous cart to merge; the session still opens."""
    events: list[str] = []
    merge = _RecordingCartMerge(events, CartMergeResult(merged=True, message=CART_MERGED_MESSAGE))
    answer = await _sessions(merge=merge).login("a@b.pe", "secret123")
    assert merge.calls == []
    assert "carritoFusionado" not in answer
    assert answer["accessToken"] == "acc-token"


@pytest.mark.asyncio
async def test_login_with_anonymous_cart_returns_message_and_adjustments() -> None:
    """SPEC-03 Req. 4: the session answer carries the notice the chat renders."""
    adjustment = {"sku": "SKU-A", "solicitado": 12, "ajustado": 10, "motivo": "TOPE_POR_LINEA"}
    merge = _RecordingCartMerge(
        [],
        CartMergeResult(
            merged=True, message=CART_MERGED_MESSAGE, adjustments=(adjustment,)
        ),
    )
    answer = await _sessions(merge=merge).login(
        "a@b.pe", "secret123", anonymous_sid=HASHED_SID
    )
    assert answer["carritoFusionado"] is True
    assert answer["avisoCarrito"] == CART_MERGED_MESSAGE
    assert answer["ajustesCarrito"] == [adjustment]
    assert merge.calls == [(HASHED_SID, CUSTOMER_ID)]


@pytest.mark.asyncio
async def test_verify_mfa_with_anonymous_cart_merges_and_returns_notice() -> None:
    """SPEC-03 Req. 2 + 4: the merge happens when the OTP finishes the login."""
    merge = _RecordingCartMerge(
        [], CartMergeResult(merged=True, message=CART_MERGED_MESSAGE)
    )
    answer = await _sessions(merge=merge).verify_mfa_code(
        "chg-1", "123456", anonymous_sid=HASHED_SID
    )
    assert answer["carritoFusionado"] is True
    assert answer["avisoCarrito"] == CART_MERGED_MESSAGE
    assert merge.calls == [(HASHED_SID, CUSTOMER_ID)]


@pytest.mark.asyncio
async def test_verify_mfa_without_anonymous_sid_never_calls_cart_merge() -> None:
    merge = _RecordingCartMerge(
        [], CartMergeResult(merged=True, message=CART_MERGED_MESSAGE)
    )
    await _sessions(merge=merge).verify_mfa_code("chg-1", "123456")
    assert merge.calls == []


@pytest.mark.asyncio
async def test_merge_runs_before_linking_conversations_and_commits_once() -> None:
    """Ordering invariant: `link_anonymous_to_customer` clears the sid the cart needs."""
    events: list[str] = []
    merge = _RecordingCartMerge(events, CartMergeResult(merged=True, message=CART_MERGED_MESSAGE))
    conversations = _RecordingConversations(events)
    uow = _FakeUow(events)

    answer = await _sessions(
        merge=merge, conversations=conversations, uow=uow
    ).login("a@b.pe", "secret123", anonymous_sid=HASHED_SID)

    assert events == ["merge", "link", "commit"]
    assert conversations.calls == [(HASHED_SID, CUSTOMER_ID)]
    assert answer["accionPendiente"] == "INICIAR_CHECKOUT"


@pytest.mark.asyncio
async def test_merge_failure_rolls_back_and_never_links_conversations() -> None:
    """A failed merge aborts the login, keeping the anonymous cart for a retry."""
    events: list[str] = []
    merge = _RecordingCartMerge(events, RuntimeError("cart merge exploded"))
    conversations = _RecordingConversations(events)
    uow = _FakeUow(events)

    with pytest.raises(RuntimeError):
        await _sessions(merge=merge, conversations=conversations, uow=uow).login(
            "a@b.pe", "secret123", anonymous_sid=HASHED_SID
        )

    assert events == ["merge", "rollback"]
    assert conversations.calls == []
    assert uow.commits == 0


# --- merge rules exercised through the real mod-cart service ----------------


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
        self.customer_cart = _Cart(customer_id=customer_id)
        return self.customer_cart

    async def cambiar_estado(self, cart_id: uuid.UUID, status: str):
        self.status_updates.append((cart_id, status))
        for cart in self.anon_carts:
            if cart.id == cart_id:
                cart.status = status
                return cart
        return None

    async def set_totals(self, cart_id: uuid.UUID, snapshot):
        self.totals_updates.append((cart_id, snapshot))


class _Cart:
    def __init__(self, *, customer_id=None, status="ACTIVE") -> None:
        self.id = uuid.uuid4()
        self.customer_id = customer_id
        self.status = status


class _Item:
    def __init__(self, cart_id: uuid.UUID, sku: str, quantity: int, price: float = 100.0) -> None:
        self.id = uuid.uuid4()
        self.cart_id = cart_id
        self.product_id = f"prod-{sku}"
        self.sku = sku
        self.name = f"Producto {sku}"
        self.variant_description = "M / Negro"
        self.image_url = None
        self.quantity = quantity
        self.unit_price_ref = price


class _FakeItems:
    def __init__(self, by_cart: dict[uuid.UUID, list[_Item]]) -> None:
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
        item = _Item(cart_id, sku, quantity, unit_price_ref)
        item.product_id = product_id
        item.name = name
        item.variant_description = variant_description
        item.image_url = image_url
        self.by_cart.setdefault(cart_id, []).append(item)
        return item

    async def set_quantity(self, item_id: uuid.UUID, *, quantity: int, unit_price_ref: float):
        for rows in self.by_cart.values():
            for row in rows:
                if row.id == item_id:
                    row.quantity = quantity
                    row.unit_price_ref = unit_price_ref


class _FakeInventory:
    def __init__(self, stock_by_sku: dict[str, int]) -> None:
        self._stock = dict(stock_by_sku)

    async def verify_quantities(self, lines, *, channel_id="CHATBOT", correlation_id=None):
        return [
            {
                "sku": line["sku"],
                "requested_quantity": line["quantity"],
                "result": (
                    "SUFICIENTE"
                    if int(line["quantity"]) <= self._stock.get(line["sku"], 100)
                    else "INSUFICIENTE"
                ),
            }
            for line in lines
        ]


def _cart_pair(anon_items: list[tuple[str, int]], customer_items: list[tuple[str, int]]):
    anon_cart = _Cart()
    customer_cart = _Cart(customer_id=CUSTOMER_ID)
    items = _FakeItems(
        {
            anon_cart.id: [_Item(anon_cart.id, sku, qty) for sku, qty in anon_items],
            customer_cart.id: [
                _Item(customer_cart.id, sku, qty) for sku, qty in customer_items
            ],
        }
    )
    carts = _FakeCarts(anon_carts=[anon_cart], customer_cart=customer_cart)
    return carts, items, anon_cart, customer_cart


def _real_merge_login(
    carts: _FakeCarts,
    items: _FakeItems,
    stock: dict[str, int],
) -> ManageSessionUseCase:
    return _sessions(
        merge=MergeAnonymousCartService(carts, items, _FakeInventory(stock)),
        conversations=_RecordingConversations([]),
        uow=_FakeUow(),
    )


@pytest.mark.asyncio
async def test_login_merge_sums_duplicate_skus_and_keeps_existing_lines() -> None:
    """SPEC-03 Req. 4 - Scenario: Ambos carritos con ítems (2+1 SKU-A, 1 SKU-B)."""
    carts, items, anon_cart, customer_cart = _cart_pair(
        anon_items=[("SKU-A", 2)],
        customer_items=[("SKU-A", 1), ("SKU-B", 1)],
    )
    sessions = _real_merge_login(carts, items, {"SKU-A": 10, "SKU-B": 10})

    answer = await sessions.login("a@b.pe", "secret123", anonymous_sid=HASHED_SID)

    merged = {item.sku: item.quantity for item in items.by_cart[customer_cart.id]}
    assert merged == {"SKU-A": 3, "SKU-B": 1}
    assert carts.status_updates == [(anon_cart.id, "MERGED")]
    assert answer["carritoFusionado"] is True
    assert answer["avisoCarrito"] == CART_MERGED_MESSAGE
    assert answer["ajustesCarrito"] == []


@pytest.mark.asyncio
async def test_login_merge_caps_each_line_at_ten_units() -> None:
    """SPEC-11 limit: the merged line never exceeds 10 units."""
    carts, items, _, customer_cart = _cart_pair(
        anon_items=[("SKU-A", 9)],
        customer_items=[("SKU-A", 5)],
    )
    sessions = _real_merge_login(carts, items, {"SKU-A": 20})

    answer = await sessions.login("a@b.pe", "secret123", anonymous_sid=HASHED_SID)

    assert items.by_cart[customer_cart.id][0].quantity == MAX_QTY_PER_LINE
    assert answer["ajustesCarrito"] == [
        {"sku": "SKU-A", "solicitado": 14, "ajustado": MAX_QTY_PER_LINE,
         "motivo": "TOPE_POR_LINEA"}
    ]


@pytest.mark.asyncio
async def test_login_merge_never_exceeds_twenty_distinct_lines() -> None:
    """SPEC-11 limit: a new SKU is dropped (and reported) when the cart is full."""
    full_cart = [(f"SKU-{index}", 1) for index in range(MAX_LINES)]
    carts, items, _, customer_cart = _cart_pair(
        anon_items=[("SKU-OVERFLOW", 1)],
        customer_items=full_cart,
    )
    sessions = _real_merge_login(carts, items, {})

    answer = await sessions.login("a@b.pe", "secret123", anonymous_sid=HASHED_SID)

    merged = {item.sku for item in items.by_cart[customer_cart.id]}
    assert len(merged) == MAX_LINES
    assert "SKU-OVERFLOW" not in merged
    assert answer["ajustesCarrito"] == [
        {"sku": "SKU-OVERFLOW", "solicitado": 1, "ajustado": 0,
         "motivo": "LIMITE_LINEAS"}
    ]


@pytest.mark.asyncio
async def test_login_merge_clamps_to_available_stock_and_omits_out_of_stock_lines() -> None:
    """SPEC-03 Req. 4 + SPEC-10: clamp to available stock; drop new OOS lines."""
    carts, items, _, customer_cart = _cart_pair(
        anon_items=[("SKU-LOW", 5), ("SKU-ZERO", 2)],
        customer_items=[("SKU-LOW", 2)],
    )
    sessions = _real_merge_login(carts, items, {"SKU-LOW": 4, "SKU-ZERO": 0})

    answer = await sessions.login("a@b.pe", "secret123", anonymous_sid=HASHED_SID)

    merged = {item.sku: item.quantity for item in items.by_cart[customer_cart.id]}
    assert merged == {"SKU-LOW": 4}
    adjustments = {row["sku"]: row for row in answer["ajustesCarrito"]}
    assert adjustments["SKU-LOW"] == {
        "sku": "SKU-LOW", "solicitado": 7, "ajustado": 4, "motivo": "STOCK_INSUFICIENTE"
    }
    assert adjustments["SKU-ZERO"] == {
        "sku": "SKU-ZERO", "solicitado": 2, "ajustado": 0, "motivo": "SIN_STOCK"
    }
