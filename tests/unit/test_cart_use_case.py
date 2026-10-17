"""Cart use case flows (SPEC-11, SPEC-10). Fakes only: no network, no database."""
import uuid

import pytest
from sqlalchemy.orm.exc import StaleDataError

from src.adapters.outbound.persistence.models.cart_item import CartItem
from src.application.use_cases.manage_cart_use_case import (
    CartError,
    ManageCartUseCase,
)
from src.domain.value_objects.caller_identity import CallerIdentity
from tests.fakes.fake_commerce import (
    FakeCartRow,
    FakeCarts,
    FakeCatalogPort,
    FakeConversations,
    FakeInventoryPort,
    FakeItems,
    FakeMessages,
    FakeUow,
)

CUSTOMER_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()
SID = "anon-sid-hash"


def make_caller(customer=True):
    return (
        CallerIdentity(customer_id=CUSTOMER_ID)
        if customer else CallerIdentity(anonymous_sid=SID)
    )


def make_cart_use_case(statuses=None, uow=None):
    conversations = FakeConversations()
    cart_uc = ManageCartUseCase(
        FakeCarts(), FakeItems(), FakeCatalogPort(),
        FakeInventoryPort(statuses or {}), conversations, FakeMessages(),
        uow or FakeUow(),
    )
    return cart_uc, conversations


def owned(cart_uc, conversations, conversation_id=CONVERSATION_ID, customer=True):
    if customer:
        conversations.add_conversation(
            conversation_id, customer_id=CUSTOMER_ID, context={})
    else:
        conversations.add_conversation(
            conversation_id, anonymous_sid=SID, context={})


@pytest.mark.asyncio
async def test_add_by_sku_creates_line_and_audits():
    cart_uc, conversations = make_cart_use_case(
        {"NIK-REV7-42-NEG": "DISPONIBLE"})
    owned(cart_uc, conversations)
    result = await cart_uc.add(
        make_caller(), CONVERSATION_ID, sku="NIK-REV7-42-NEG", quantity=2)
    assert result["code"] == "OK"
    assert result["bloques"][0]["tipo"] == "CARRITO"
    assert "Agregué" in result["texto"]
    assert len(cart_uc._messages.rows) == 1
    assert cart_uc._messages.rows[0]["tool_name"] == "agregar_al_carrito"


@pytest.mark.asyncio
async def test_add_same_sku_sums_quantity():
    cart_uc, conversations = make_cart_use_case(
        {"NIK-REV7-42-NEG": "DISPONIBLE"})
    owned(cart_uc, conversations)
    await cart_uc.add(make_caller(), CONVERSATION_ID,
                      sku="NIK-REV7-42-NEG", quantity=1)
    await cart_uc.add(make_caller(), CONVERSATION_ID,
                      sku="NIK-REV7-42-NEG", quantity=1)
    view = await cart_uc.view(make_caller(), CONVERSATION_ID)
    assert view["bloques"][0]["datos"]["lineas"][0]["cantidad"] == 2


@pytest.mark.asyncio
async def test_add_over_line_limit_rejects_with_max_addable():
    cart_uc, conversations = make_cart_use_case(
        {"NIK-REV7-42-NEG": "DISPONIBLE"})
    owned(cart_uc, conversations)
    await cart_uc.add(make_caller(), CONVERSATION_ID,
                      sku="NIK-REV7-42-NEG", quantity=9)
    with pytest.raises(CartError) as error:
        await cart_uc.add(make_caller(), CONVERSATION_ID,
                          sku="NIK-REV7-42-NEG", quantity=3)
    assert error.value.code == "LIMITE_CANTIDAD"
    assert error.value.data["max_agregable"] == 1


@pytest.mark.asyncio
async def test_add_out_of_stock_rejects():
    cart_uc, conversations = make_cart_use_case(
        {"NIK-REV7-42-NEG": "AGOTADO"})
    owned(cart_uc, conversations)
    with pytest.raises(CartError) as error:
        await cart_uc.add(make_caller(), CONVERSATION_ID,
                          sku="NIK-REV7-42-NEG", quantity=1)
    assert error.value.code in ("SKU_AGOTADO", "STOCK_INSUFICIENTE")


@pytest.mark.asyncio
async def test_update_to_zero_removes_line():
    cart_uc, conversations = make_cart_use_case(
        {"NIK-REV7-42-NEG": "DISPONIBLE"})
    owned(cart_uc, conversations)
    await cart_uc.add(make_caller(), CONVERSATION_ID,
                      sku="NIK-REV7-42-NEG", quantity=1)
    view = await cart_uc.view(make_caller(), CONVERSATION_ID)
    item_id = view["bloques"][0]["datos"]["lineas"][0]["item_id"]
    result = await cart_uc.update_quantity(
        make_caller(), CONVERSATION_ID, item_id=item_id, quantity=0)
    assert "Quité" in result["texto"]
    empty = await cart_uc.view(make_caller(), CONVERSATION_ID)
    assert empty["bloques"] == []


@pytest.mark.asyncio
async def test_ambiguous_name_reports_options():
    cart_uc, conversations = make_cart_use_case()
    owned(cart_uc, conversations)
    cart = await cart_uc._carts.guardar(FakeCartRow(customer_id=CUSTOMER_ID))
    for name in ("Zapatillas Nike", "Zapatillas Adidas"):
        await cart_uc._items.guardar(CartItem(
            cart_id=cart.id, product_id="prod-x", sku=f"SKU-{name}",
            name=name, quantity=1, unit_price_ref=10.0,
        ))
    with pytest.raises(CartError) as error:
        await cart_uc.remove_item(
            make_caller(), CONVERSATION_ID, name="zapatillas")
    assert error.value.code == "LINEA_AMBIGUA"
    assert len(error.value.data["opciones"]) == 2


@pytest.mark.asyncio
async def test_concurrent_modification_retries_once():
    uow = FakeUow(fail_commits=1, fail_with=StaleDataError("stale"))
    cart_uc, conversations = make_cart_use_case(
        {"NIK-REV7-42-NEG": "DISPONIBLE"}, uow=uow)
    owned(cart_uc, conversations)
    result = await cart_uc.add(make_caller(), CONVERSATION_ID,
                               sku="NIK-REV7-42-NEG", quantity=1)
    assert result["code"] == "OK"
    assert uow.commits >= 1


@pytest.mark.asyncio
async def test_anonymous_cart_is_per_conversation():
    cart_uc, conversations = make_cart_use_case(
        {"NIK-REV7-42-NEG": "DISPONIBLE"})
    first, second = uuid.uuid4(), uuid.uuid4()
    owned(cart_uc, conversations, first, customer=False)
    owned(cart_uc, conversations, second, customer=False)
    caller = make_caller(customer=False)
    await cart_uc.add(caller, first, sku="NIK-REV7-42-NEG", quantity=1)
    assert (await cart_uc.view(caller, second))["bloques"] == []
