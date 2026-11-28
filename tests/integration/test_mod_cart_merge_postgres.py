"""PostgreSQL 18 integration tests for anonymous cart merge on login (SPEC-03 Req. 4, #184)."""
from __future__ import annotations

import uuid

import pytest

from src.modules.mod_cart.adapters.cart_item_postgres_adapter import CartItemPostgresAdapter
from src.modules.mod_cart.adapters.cart_postgres_adapter import CartPostgresAdapter
from src.modules.mod_cart.application.merge_anonymous_cart_service import (
    CART_MERGED_MESSAGE,
    MergeAnonymousCartService,
)
from src.shared.infrastructure.db.models import Conversation
from src.shared.infrastructure.http.dependencies.caller_identity import hash_chat_sid
from src.shared.infrastructure.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class _ScriptedInventory:
    def __init__(self, max_by_sku: dict[str, int]) -> None:
        self._max = dict(max_by_sku)

    async def verify_quantities(self, lines, *, channel_id="CHATBOT", correlation_id=None):
        return [
            {
                "sku": line["sku"],
                "requested_quantity": line["quantity"],
                "result": (
                    "SUFICIENTE"
                    if int(line["quantity"]) <= self._max.get(line["sku"], 10)
                    else "INSUFICIENTE"
                ),
            }
            for line in lines
        ]


async def test_merge_anonymous_cart_with_real_postgres_constraints(postgres_session) -> None:
    customer_id = uuid.uuid4()
    hashed_sid = hash_chat_sid(f"raw-sid-{uuid.uuid4()}")

    conv_adapter = ConversationPostgresAdapter(postgres_session)
    cart_adapter = CartPostgresAdapter(postgres_session)
    item_adapter = CartItemPostgresAdapter(postgres_session)

    conv = await conv_adapter.guardar(
        Conversation(anonymous_sid=hashed_sid, status="ACTIVE")
    )
    anon_cart = await cart_adapter.crear_anonimo(conv.id)
    await item_adapter.agregar(
        cart_id=anon_cart.id,
        product_id="p-a",
        sku="SKU-A",
        name="Polo A",
        variant_description="M",
        image_url=None,
        quantity=4,
        unit_price_ref=50.0,
    )
    await item_adapter.agregar(
        cart_id=anon_cart.id,
        product_id="p-b",
        sku="SKU-B",
        name="Short B",
        variant_description="L",
        image_url=None,
        quantity=1,
        unit_price_ref=80.0,
    )

    cust_cart = await cart_adapter.crear_para_cliente(customer_id)
    await cart_adapter.set_totals(cust_cart.id, {"subtotal": 100.0, "total": 100.0})
    await item_adapter.agregar(
        cart_id=cust_cart.id,
        product_id="p-a",
        sku="SKU-A",
        name="Polo A",
        variant_description="M",
        image_url=None,
        quantity=2,
        unit_price_ref=50.0,
    )
    await postgres_session.commit()

    # Live stock caps SKU-A at 5 (2 + 4 = 6 -> clamped to 5).
    svc = MergeAnonymousCartService(
        carts=cart_adapter,
        items=item_adapter,
        inventory=_ScriptedInventory({"SKU-A": 5, "SKU-B": 10}),
    )
    result = await svc.merge_for_login(hashed_sid, customer_id)
    await postgres_session.commit()

    assert result.merged is True
    assert result.message == CART_MERGED_MESSAGE
    assert any(
        adj["sku"] == "SKU-A" and adj["ajustado"] == 5 and adj["motivo"] == "STOCK_INSUFICIENTE"
        for adj in result.adjustments
    )

    # Verify DB state: anonymous cart is MERGED, totals_snapshot is SQL NULL (constraint passes!).
    reloaded_anon = await cart_adapter.obtener_por_id(anon_cart.id)
    assert reloaded_anon is not None
    assert reloaded_anon.status == "MERGED"

    reloaded_cust = await cart_adapter.obtener_por_id(cust_cart.id)
    assert reloaded_cust is not None
    assert reloaded_cust.totals_snapshot is None

    merged_items = await item_adapter.listar_por_cart(cust_cart.id)
    assert {i.sku: i.quantity for i in merged_items} == {"SKU-A": 5, "SKU-B": 1}

    # Idempotent: second login does not merge again.
    second = await svc.merge_for_login(hashed_sid, customer_id)
    assert second.merged is False
