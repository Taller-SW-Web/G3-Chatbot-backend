"""Commercial handlers through the real tool registry (SPEC-05 Req. 3, 6, 8).

Handlers come from the catalog/cart use cases over fakes; the registry
validates schemas, strips caller identity and enforces confirmation.
No network, no database.
"""
import uuid

import pytest

from src.application.tools.cart_handlers import build_cart_handlers
from src.application.tools.catalog_handlers import build_catalog_handlers
from src.application.tools.catalog_tools import build_tool_registry
from src.application.tools.tool_registry import ExecutionContext
from src.application.use_cases.manage_cart_use_case import ManageCartUseCase
from src.application.use_cases.manage_catalog_use_case import ManageCatalogUseCase
from src.domain.value_objects.caller_identity import CallerIdentity
from tests.fakes.fake_commerce import (
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
STATUSES = {
    "NIK-REV7-42-NEG": "DISPONIBLE",
    "NIK-REV7-42-BLA": "DISPONIBLE",
    "NIK-REV7-43-NEG": "DISPONIBLE",
}


def make_registry():
    conversations = FakeConversations()
    conversations.add_conversation(
        CONVERSATION_ID, customer_id=CUSTOMER_ID, context={})
    catalog_uc = ManageCatalogUseCase(
        FakeCatalogPort(), FakeInventoryPort(dict(STATUSES)), conversations,
        FakeUow(),
    )
    cart_uc = ManageCartUseCase(
        FakeCarts(), FakeItems(), FakeCatalogPort(),
        FakeInventoryPort(dict(STATUSES)), conversations, FakeMessages(),
        FakeUow(),
    )
    handlers = {}
    handlers.update(build_catalog_handlers(catalog_uc))
    handlers.update(build_cart_handlers(cart_uc))
    context = ExecutionContext(
        conversacion_id=str(CONVERSATION_ID), cliente_id=str(CUSTOMER_ID),
        chat_sid=None,
    )
    return build_tool_registry(handlers), context


@pytest.mark.asyncio
async def test_search_registers_carousel_in_memory():
    registry, context = make_registry()
    result = await registry.ejecutar(
        "buscar_productos", {"q": "Nike"}, context)
    assert result.code == "OK"
    assert result.data["bloques"][0]["tipo"] == "CARRUSEL_PRODUCTOS"


@pytest.mark.asyncio
async def test_add_by_carousel_ordinal_end_to_end():
    registry, context = make_registry()
    await registry.ejecutar("buscar_productos", {"q": "Revolution"}, context)
    question = await registry.ejecutar(
        "agregar_al_carrito", {"productoRef": 1, "cantidad": 1}, context)
    assert question.code == "OK"
    assert question.data["bloques"][0]["tipo"] == "SELECTOR_VARIANTE"
    result = await registry.ejecutar(
        "agregar_al_carrito",
        {"productoRef": 1, "talla": "42", "color": "negro", "cantidad": 1},
        context)
    assert result.code == "OK"
    assert "Agregué" in result.data["texto"]
    view = await registry.ejecutar("ver_carrito", {}, context)
    assert view.data["bloques"][0]["tipo"] == "CARRITO"


@pytest.mark.asyncio
async def test_llm_path_cannot_clear_without_confirmation():
    registry, context = make_registry()
    await registry.ejecutar(
        "agregar_al_carrito", {"sku": "NIK-REV7-42-NEG", "cantidad": 1},
        context)
    result = await registry.ejecutar("vaciar_carrito", {}, context)
    assert result.code == "REQUIERE_CONFIRMACION"


@pytest.mark.asyncio
async def test_confirmed_action_clears_through_dispatcher():
    registry, context = make_registry()
    await registry.ejecutar(
        "agregar_al_carrito", {"sku": "NIK-REV7-42-NEG", "cantidad": 1},
        context)
    result = await registry.ejecutar_accion(
        "vaciar_carrito", {}, context, confirmacion_ui=True)
    assert result.code == "OK"
    view = await registry.ejecutar("ver_carrito", {}, context)
    assert view.data["bloques"] == []


@pytest.mark.asyncio
async def test_invalid_arguments_do_not_reach_handlers():
    registry, context = make_registry()
    result = await registry.ejecutar(
        "cambiar_cantidad", {"itemId": "x", "cantidad": -3}, context)
    assert result.code == "ARGUMENTOS_INVALIDOS"


@pytest.mark.asyncio
async def test_llm_identity_arguments_are_ignored():
    registry, context = make_registry()
    result = await registry.ejecutar(
        "ver_carrito", {"clienteId": "otro-cliente"}, context)
    assert result.code == "OK"
