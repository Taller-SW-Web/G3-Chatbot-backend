"""Cart use case flows (SPEC-11, SPEC-10). Fakes only: no network, no database."""
import logging
import uuid
from decimal import Decimal

import pytest

from src.modules.mod_cart.application.manage_cart_use_case import (
    CartError,
    ManageCartUseCase,
)
from src.modules.mod_cart.ports.cart_repository_port import StaleCartError
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.domain.module_errors import ModuleRequestError, ModuleUnavailableError
from src.shared.domain.request_context import correlation_id_var
from tests.fakes.fake_commerce import (
    FakeCarts,
    FakeCatalogPort,
    FakeConversationContext,
    FakeInventoryPort,
    FakeItems,
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
    context = FakeConversationContext()
    cart_uc = ManageCartUseCase(
        FakeCarts(), FakeItems(), FakeCatalogPort(),
        FakeInventoryPort(statuses or {}, default="DISPONIBLE"), context,
        uow or FakeUow(),
    )
    return cart_uc, context


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
    assert len(cart_uc._conversation_context.messages.rows) == 1
    assert cart_uc._conversation_context.messages.rows[0]["tool_name"] == "agregar_al_carrito"


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
    [block] = empty["bloques"]
    assert block["tipo"] == "CARRITO"
    assert block["datos"]["lineas"] == []
    assert block["datos"]["subtotal"] == 0 and block["datos"]["total"] == 0


@pytest.mark.asyncio
async def test_ambiguous_name_reports_options():
    cart_uc, conversations = make_cart_use_case()
    owned(cart_uc, conversations)
    cart = await cart_uc._carts.crear_para_cliente(CUSTOMER_ID)
    for name in ("Zapatillas Nike", "Zapatillas Adidas"):
        await cart_uc._items.agregar(
            cart_id=cart.id, product_id="prod-x", sku=f"SKU-{name}",
            name=name, quantity=1, unit_price_ref=10.0,
        )
    with pytest.raises(CartError) as error:
        await cart_uc.remove_item(
            make_caller(), CONVERSATION_ID, name="zapatillas")
    assert error.value.code == "LINEA_AMBIGUA"
    assert len(error.value.data["opciones"]) == 2


@pytest.mark.asyncio
async def test_concurrent_modification_retries_once():
    uow = FakeUow(fail_commits=1, fail_with=StaleCartError("stale"))
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
    [empty] = (await cart_uc.view(caller, second))["bloques"]
    assert empty["tipo"] == "CARRITO" and empty["datos"]["lineas"] == []


@pytest.mark.asyncio
async def test_customer_cannot_touch_a_foreign_conversation():
    """A customer must not reach another user's conversation memory/audit."""
    cart_uc, conversations = make_cart_use_case(
        {"NIK-REV7-42-NEG": "DISPONIBLE"})
    conversations.add_conversation(
        CONVERSATION_ID, customer_id=uuid.uuid4(), context={})
    with pytest.raises(CartError) as error:
        await cart_uc.add(make_caller(), CONVERSATION_ID,
                          sku="NIK-REV7-42-NEG", quantity=1)
    assert error.value.code == "RECURSO_NO_ENCONTRADO"
    assert cart_uc._conversation_context.messages.rows == []


@pytest.mark.asyncio
async def test_add_requires_variant_when_attributes_are_missing():
    """POST without talla/color on a variant product asks for the selector."""
    cart_uc, conversations = make_cart_use_case(
        {"NIK-REV7-42-NEG": "DISPONIBLE"})
    owned(cart_uc, conversations)
    result = await cart_uc.add(
        make_caller(), CONVERSATION_ID, product_id="prod-001", quantity=1)
    assert result["requiere_variante"] is True
    assert result["product_id"] == "prod-001"
    assert set(result["faltantes"]) == {"talla", "color"}
    assert result["bloques"][0]["tipo"] == "SELECTOR_VARIANTE"
    assert result["bloques"][0]["datos"]["seleccion"] == {}


@pytest.mark.asyncio
async def test_add_selector_keeps_known_size():
    cart_uc, conversations = make_cart_use_case(
        {"NIK-REV7-42-NEG": "DISPONIBLE"})
    owned(cart_uc, conversations)
    result = await cart_uc.add(
        make_caller(), CONVERSATION_ID, product_id="prod-001", size="42",
        quantity=1)
    assert result["faltantes"] == ["color"]
    assert result["seleccion"] == {"talla": "42"}
    assert result["bloques"][0]["datos"]["seleccion"] == {"talla": "42"}


@pytest.mark.asyncio
async def test_first_concurrent_add_maps_unique_clash_to_conflict():
    """Two carts opened at once clash on the unique key -> CONFLICTO, not 503."""
    from src.modules.mod_cart.ports.cart_repository_port import CartConflictError

    cart_uc, conversations = make_cart_use_case(
        {"NIK-REV7-42-NEG": "DISPONIBLE"})
    owned(cart_uc, conversations)
    cart_uc._items = FakeItems(fail_flush_with=CartConflictError("dup"))
    with pytest.raises(CartError) as error:
        await cart_uc.add(make_caller(), CONVERSATION_ID,
                          sku="NIK-REV7-42-NEG", quantity=1)
    assert error.value.code == "CONFLICTO"


@pytest.mark.asyncio
async def test_add_by_name_without_attributes_asks_for_variant():
    """A named product outside the last carousel still reaches the variant selector."""
    cart_uc, conversations = make_cart_use_case(
        {"NIK-REV7-42-NEG": "DISPONIBLE"})
    owned(cart_uc, conversations)
    result = await cart_uc.add(
        make_caller(), CONVERSATION_ID, name="Nike Revolution 7", quantity=1)
    assert result["requiere_variante"] is True
    assert result["product_id"] == "prod-001"
    assert set(result["faltantes"]) == {"talla", "color"}
    assert result["bloques"][0]["tipo"] == "SELECTOR_VARIANTE"


@pytest.mark.asyncio
async def test_add_by_name_with_size_and_color_adds_the_line():
    cart_uc, conversations = make_cart_use_case(
        {"NIK-REV7-42-NEG": "DISPONIBLE"})
    owned(cart_uc, conversations)
    result = await cart_uc.add(
        make_caller(), CONVERSATION_ID, name="Nike Revolution 7",
        size="42", color="negro", quantity=1)
    assert "requiere_variante" not in result
    lines = result["bloques"][0]["datos"]["lineas"]
    assert [line["sku"] for line in lines] == ["NIK-REV7-42-NEG"]


@pytest.mark.asyncio
async def test_add_by_unknown_name_returns_the_not_found_answer():
    cart_uc, conversations = make_cart_use_case()
    owned(cart_uc, conversations)
    with pytest.raises(CartError) as error:
        await cart_uc.add(
            make_caller(), CONVERSATION_ID, name="zzz inexistente", quantity=1)
    assert error.value.code == "PRODUCTO_NO_DISPONIBLE"
    assert "No encontré un producto llamado zzz inexistente" in error.value.detail


@pytest.mark.asyncio
async def test_add_by_ambiguous_name_asks_which_product():
    cart_uc, conversations = make_cart_use_case()
    owned(cart_uc, conversations)
    with pytest.raises(CartError) as error:
        await cart_uc.add(make_caller(), CONVERSATION_ID, name="Nike", quantity=1)
    assert error.value.code == "PRODUCTO_NO_DISPONIBLE"
    assert "Encontré varios productos parecidos a Nike" in error.value.detail


def _priced_catalog(sku, regular):
    return FakeCatalogPort(prices={
        sku: {
            "precio_regular": regular,
            "precio_oferta": None,
            "currency": "PEN",
            "valid_from": "2020-01-01T00:00:00Z",
            "valid_until": None,
        },
    })


@pytest.mark.asyncio
async def test_view_commits_snapshot_and_last_seen_price():
    """GET /carrito is a write-through view: commit the snapshot it showed."""
    sku = "NIK-REV7-42-NEG"
    catalog = _priced_catalog(sku, 10.0)
    carts, items, uow = FakeCarts(), FakeItems(), FakeUow()
    cart_uc = ManageCartUseCase(
        carts, items, catalog, FakeInventoryPort({sku: "DISPONIBLE"}),
        FakeConversationContext(), uow,
    )
    cart = await carts.crear_para_cliente(CUSTOMER_ID)
    row = await items.agregar(
        cart_id=cart.id, product_id="prod-1", sku=sku, name="Zapatilla",
        quantity=1, unit_price_ref=10.0,
    )
    catalog._prices[sku]["precio_regular"] = 12.0

    view = await cart_uc.view(make_caller(), None)

    assert row.unit_price_ref == 12.0
    assert view["bloques"][0]["datos"]["total"] == 12.0
    snapshot = carts.rows[cart.id].totals_snapshot
    assert snapshot["promociones_ok"] is True
    assert Decimal(snapshot["subtotal"]) == Decimal("12")
    assert uow.commits == 1


@pytest.mark.asyncio
async def test_recalculate_totals_refreshes_snapshot_and_commits():
    """Checkout recovery uses this path: fresh prices, promotions and snapshot."""
    sku = "NIK-REV7-42-NEG"
    catalog = _priced_catalog(sku, 10.0)
    carts, items, uow = FakeCarts(), FakeItems(), FakeUow()
    cart_uc = ManageCartUseCase(
        carts, items, catalog, FakeInventoryPort({sku: "DISPONIBLE"}),
        FakeConversationContext(), uow,
    )
    cart = await carts.crear_para_cliente(CUSTOMER_ID)
    await items.agregar(
        cart_id=cart.id, product_id="prod-1", sku=sku, name="Zapatilla",
        quantity=1, unit_price_ref=10.0,
    )
    catalog._prices[sku]["precio_regular"] = 12.0

    datos = await cart_uc.recalculate_totals(cart)

    assert datos["subtotal"] == 12.0
    assert datos["promociones_ok"] is True
    snapshot = carts.rows[cart.id].totals_snapshot
    assert snapshot["promociones_ok"] is True
    assert Decimal(snapshot["total"]) == Decimal("12")
    assert uow.commits == 1


@pytest.mark.asyncio
async def test_view_persists_unavailable_promotions_instead_of_zero_discount():
    """A failed evaluation is not a valid zero-discount snapshot."""
    sku = "NIK-REV7-42-NEG"

    class BrokenPromotionsCatalog(FakeCatalogPort):
        async def evaluate_promotions(self, *args, **kwargs):
            raise TimeoutError("promociones caídas")

    carts, items, uow = FakeCarts(), FakeItems(), FakeUow()
    cart_uc = ManageCartUseCase(
        carts,
        items,
        BrokenPromotionsCatalog(prices={
            sku: {
                "precio_regular": 10.0,
                "precio_oferta": None,
                "currency": "PEN",
                "valid_from": "2020-01-01T00:00:00Z",
                "valid_until": None,
            },
        }),
        FakeInventoryPort({sku: "DISPONIBLE"}),
        FakeConversationContext(),
        uow,
    )
    cart = await carts.crear_para_cliente(CUSTOMER_ID)
    await items.agregar(
        cart_id=cart.id, product_id="prod-1", sku=sku, name="Zapatilla",
        quantity=1, unit_price_ref=10.0,
    )

    view = await cart_uc.view(make_caller(), None)

    datos = view["bloques"][0]["datos"]
    assert datos["promociones_ok"] is False
    assert any("checkout queda bloqueado" in aviso for aviso in datos["avisos"])
    snapshot = carts.rows[cart.id].totals_snapshot
    assert snapshot["promociones_ok"] is False
    assert Decimal(snapshot["total"]) == Decimal("10")


@pytest.mark.asyncio
async def test_view_persists_the_request_correlation_id_in_the_snapshot():
    """The snapshot must let an incident find the G6 evaluation request (issue #181)."""
    sku = "NIK-REV7-42-NEG"
    carts, items, uow = FakeCarts(), FakeItems(), FakeUow()
    cart_uc = ManageCartUseCase(
        carts, items, _priced_catalog(sku, 10.0),
        FakeInventoryPort({sku: "DISPONIBLE"}), FakeConversationContext(), uow,
    )
    cart = await carts.crear_para_cliente(CUSTOMER_ID)
    await items.agregar(
        cart_id=cart.id, product_id="prod-1", sku=sku, name="Zapatilla",
        quantity=1, unit_price_ref=10.0,
    )
    correlation_id = str(uuid.uuid4())
    token = correlation_id_var.set(correlation_id)
    try:
        await cart_uc.view(make_caller(), None)
    finally:
        correlation_id_var.reset(token)

    snapshot = carts.rows[cart.id].totals_snapshot
    assert snapshot["correlation_id"] == correlation_id
    assert snapshot["promociones_ok"] is True


@pytest.mark.asyncio
async def test_view_logs_promotion_failures_with_module_status_and_correlation(caplog):
    """A degraded promotion engine stays visible: module, status/code and request id."""
    sku = "NIK-REV7-42-NEG"

    class RejectedPromotionsCatalog(FakeCatalogPort):
        async def evaluate_promotions(self, *args, **kwargs):
            raise ModuleRequestError("products", 400, "PROMOCIONES_INVALIDAS")

    carts, items = FakeCarts(), FakeItems()
    cart_uc = ManageCartUseCase(
        carts, items, RejectedPromotionsCatalog(), FakeInventoryPort({sku: "DISPONIBLE"}),
        FakeConversationContext(), FakeUow(),
    )
    cart = await carts.crear_para_cliente(CUSTOMER_ID)
    await items.agregar(
        cart_id=cart.id, product_id="prod-1", sku=sku, name="Zapatilla",
        quantity=1, unit_price_ref=10.0,
    )
    correlation_id = str(uuid.uuid4())
    token = correlation_id_var.set(correlation_id)
    try:
        with caplog.at_level(logging.WARNING):
            await cart_uc.view(make_caller(), None)
    finally:
        correlation_id_var.reset(token)

    record = next(r for r in caplog.records if "promotions evaluation failed" in r.getMessage())
    message = record.getMessage()
    assert "module=products" in message
    assert "status=400" in message
    assert "code=PROMOCIONES_INVALIDAS" in message
    assert f"correlation_id={correlation_id}" in message
    snapshot = carts.rows[cart.id].totals_snapshot
    assert snapshot["promociones_ok"] is False
    assert snapshot["correlation_id"] == correlation_id


@pytest.mark.asyncio
async def test_add_logs_inventory_failures_with_module_status_and_correlation(caplog):
    class BrokenInventory(FakeInventoryPort):
        async def get_availability(self, skus, **kwargs):
            raise ModuleUnavailableError("products", "timeout")

    cart_uc, conversations = make_cart_use_case()
    cart_uc._inventory = BrokenInventory()
    owned(cart_uc, conversations)
    correlation_id = str(uuid.uuid4())
    token = correlation_id_var.set(correlation_id)
    try:
        with caplog.at_level(logging.WARNING):
            with pytest.raises(CartError) as error:
                await cart_uc.add(
                    make_caller(), CONVERSATION_ID,
                    sku="NIK-REV7-42-NEG", quantity=1,
                )
    finally:
        correlation_id_var.reset(token)

    assert error.value.code == "SERVICIO_NO_DISPONIBLE"
    record = next(r for r in caplog.records if "inventory check failed" in r.getMessage())
    message = record.getMessage()
    assert "module=products" in message
    assert "status=503" in message
    assert "code=MODULO_NO_DISPONIBLE" in message
    assert f"correlation_id={correlation_id}" in message
