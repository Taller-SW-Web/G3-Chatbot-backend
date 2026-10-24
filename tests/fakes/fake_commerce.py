"""In-memory fakes for unit tests: commercial ports live in src (ADR-0022)
so deployed fake mode shares them; persistence fakes stay test-only.

`FakeCarts`/`FakeItems` implement the `CartRepository`/`CartItemRepository`
contracts (the use case never builds rows) and can be told to fail like the
driver would (`StaleCartError`, `CartConflictError`).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from src.adapters.outbound.fakes.fake_products import (
    FakeCatalogPort,
    FakeInventoryPort,
)
from src.ports.outbound.cart_repository_port import (
    CartConflictError,
    StaleCartError,
)

__all__ = [
    "FakeCatalogPort",
    "FakeInventoryPort",
    "FakeRow",
    "FakeConversations",
    "FakeCarts",
    "FakeItems",
    "FakeMessages",
    "FakeUow",
    "FakeCartRow",
    "StaleCartError",
    "CartConflictError",
]


class FakeRow:
    def __init__(self, **fields):
        self.id = fields.pop("id", uuid.uuid4())
        self.version = 0
        for key, value in fields.items():
            setattr(self, key, value)


@dataclass(frozen=True)
class FakeConversationEntity:
    id: uuid.UUID


class FakeConversations:
    def __init__(self):
        self.rows: dict[uuid.UUID, FakeRow] = {}
        self.touched: list[uuid.UUID] = []

    def add_conversation(self, conversation_id, *, customer_id=None,
                         anonymous_sid=None, context=None) -> FakeRow:
        row = FakeRow(
            id=conversation_id, customer_id=customer_id,
            anonymous_sid=anonymous_sid, context=dict(context or {}),
        )
        self.rows[conversation_id] = row
        return row

    async def find_owned(self, conversation_id, caller):
        row = self.rows.get(conversation_id)
        if row is None:
            return None
        if caller.customer_id is not None and row.customer_id != caller.customer_id:
            return None
        if caller.customer_id is None and row.anonymous_sid != caller.anonymous_sid:
            return None
        # Immutable like the production entity: it carries no `context` (memory goes through the port).
        return FakeConversationEntity(id=row.id)

    async def get_context(self, conversation_id) -> dict:
        row = self.rows.get(conversation_id)
        return dict(row.context) if row is not None else {}

    async def save_context(self, conversation_id, context) -> None:
        self.rows[conversation_id].context = dict(context)

    async def touch(self, conversation_id) -> None:
        self.touched.append(conversation_id)


class FakeCarts:
    def __init__(self, *, fail_flush_with=None):
        self.rows: dict[uuid.UUID, FakeRow] = {}
        self._fail_flush_with = fail_flush_with

    def _check_flush(self):
        if self._fail_flush_with is not None:
            exc, self._fail_flush_with = self._fail_flush_with, None
            raise exc

    def _crear(self, *, customer_id=None, conversation_id=None) -> FakeCartRow:
        row = FakeCartRow(customer_id=customer_id, conversation_id=conversation_id)
        self.rows[row.id] = row
        self._check_flush()
        return row

    async def crear_para_cliente(self, customer_id):
        return self._crear(customer_id=customer_id)

    async def crear_anonimo(self, conversation_id):
        return self._crear(conversation_id=conversation_id)

    async def obtener_por_id(self, cart_id):
        return self.rows.get(cart_id)

    async def obtener_abierto_por_cliente(self, customer_id):
        return next((
            row for row in self.rows.values()
            if row.customer_id == customer_id and row.status in ("ACTIVE", "IN_CHECKOUT")
        ), None)

    async def obtener_abierto_por_conversacion(self, conversation_id):
        return next((
            row for row in self.rows.values()
            if row.conversation_id == conversation_id
            and row.customer_id is None
            and row.status in ("ACTIVE", "IN_CHECKOUT")
        ), None)

    async def cambiar_estado(self, cart_id, status):
        row = self.rows.get(cart_id)
        if row is None:
            return None
        row.status = status
        return row

    async def set_totals(self, cart_id, snapshot):
        row = self.rows.get(cart_id)
        if row is not None:
            row.totals_snapshot = snapshot
        self._check_flush()

    async def reset_checkout(self, cart_id):
        row = self.rows.get(cart_id)
        if row is not None:
            row.coupon_code = None
            row.shipping_snapshot = None
            row.totals_snapshot = None

    async def touch(self, cart_id):
        row = self.rows.get(cart_id)
        if row is not None:
            row.updated_at = datetime.now(timezone.utc)


class FakeItems:
    def __init__(self, *, fail_flush_with=None):
        self.rows: dict[uuid.UUID, FakeRow] = {}
        self._fail_flush_with = fail_flush_with

    def _check_flush(self):
        if self._fail_flush_with is not None:
            exc, self._fail_flush_with = self._fail_flush_with, None
            raise exc

    async def obtener_por_id(self, item_id):
        return self.rows.get(item_id)

    async def obtener_por_cart_y_sku(self, cart_id, sku):
        return next((
            row for row in self.rows.values()
            if row.cart_id == cart_id and row.sku == sku
        ), None)

    async def listar_por_cart(self, cart_id):
        return [row for row in self.rows.values() if row.cart_id == cart_id]

    async def agregar(self, *, cart_id, product_id, sku, name,
                      variant_description=None, image_url=None,
                      quantity, unit_price_ref):
        row = FakeRow(
            cart_id=cart_id, product_id=product_id, sku=sku, name=name,
            variant_description=variant_description, image_url=image_url,
            quantity=quantity, unit_price_ref=unit_price_ref,
        )
        self.rows[row.id] = row
        self._check_flush()
        return row

    async def set_quantity(self, item_id, *, quantity, unit_price_ref):
        row = self.rows.get(item_id)
        if row is not None:
            row.quantity = quantity
            row.unit_price_ref = unit_price_ref
        self._check_flush()

    async def eliminar(self, item_id) -> bool:
        return self.rows.pop(item_id, None) is not None


class FakeMessages:
    def __init__(self):
        self.rows: list[dict] = []

    async def add(self, conversation_id, role, text, **extra):
        row = {"conversation_id": conversation_id, "role": role,
               "text": text, **extra}
        self.rows.append(row)
        return row


class FakeUow:
    def __init__(self, fail_commits: int = 0, fail_with=None):
        self.commits = 0
        self.rollbacks = 0
        self._fail_commits = fail_commits
        self._fail_with = fail_with

    async def commit(self) -> None:
        if self._fail_commits > 0:
            self._fail_commits -= 1
            raise self._fail_with
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeCartRow:
    """Cart model stand-in honoring the real column contract."""

    def __init__(self, *, customer_id=None, conversation_id=None):
        self.id = uuid.uuid4()
        self.customer_id = customer_id
        self.conversation_id = conversation_id
        self.status = "ACTIVE"
        self.coupon_code = None
        self.shipping_snapshot = None
        self.totals_snapshot = None
        self.version = 0
        self.updated_at = None
