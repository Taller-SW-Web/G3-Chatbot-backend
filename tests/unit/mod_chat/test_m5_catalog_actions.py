"""M5 catalog: 29 tool names, no aliases, client actions and UI-only confirmations."""

import uuid
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.modules.mod_catalog.application.tools import ClaimConfirmArguments, build_tool_registry
from src.modules.mod_chat.adapters.registry_tool_adapter import RegistryToolAdapter
from src.modules.mod_chat.application.tools.action_dispatcher import ActionDispatcher
from src.modules.mod_chat.ports.tool_executor_port import ToolExecutionContext
from src.shared.application.tools.tool_contracts import TOOL_CONTRACTS
from src.shared.application.tools.tool_registry import (
    ExecutionContext,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
)

CUSTOMER = str(uuid.uuid4())
CONTEXT = ExecutionContext(conversacion_id=str(uuid.uuid4()), cliente_id=CUSTOMER)
ANONYMOUS = ExecutionContext(conversacion_id=str(uuid.uuid4()))

CLIENT_ACTIONS = ("IR_A_CHECKOUT", "CERRAR_SESION", "NUEVO_CHAT", "BUSCAR_CHATS")

CLAIM_PAYLOAD = {
    "pedidoId": "PED-2026-00891",
    "tipo": "RECLAMO",
    "motivo": "PRODUCTO_DEFECTUOSO_O_INCORRECTO",
    "descripcion": "La suela de la zapatilla derecha se despegó al primer uso.",
    "consumidor": {
        "nombreCompleto": "Juan Pérez",
        "documento": "72458912",
        "email": "juan@example.com",
        "telefono": "+51999888777",
    },
}


def test_registry_exposes_exactly_the_29_contract_names():
    handlers = {
        contract.name: AsyncMock()
        for contract in TOOL_CONTRACTS
        if contract.name != "solicitar_registro"
    }
    registry = build_tool_registry(handlers)
    names = [tool["nombre"] for tool in registry.catalogo()]
    contract_names = {contract.name for contract in TOOL_CONTRACTS}
    assert len(names) == 29
    assert set(names) == contract_names
    assert "listar_pedidos" not in names
    assert "consultar_pedido" not in names
    assert "confirmar_reclamo" not in names
    assert "confirmar_devolucion" not in names


def test_registry_flags_match_the_contract():
    handlers = {
        contract.name: AsyncMock()
        for contract in TOOL_CONTRACTS
        if contract.name != "solicitar_registro"
    }
    registry = build_tool_registry(handlers)
    tools = {tool["nombre"]: tool for tool in registry.catalogo()}
    for name in (
        "consultar_pedidos",
        "consultar_seguimiento",
        "iniciar_checkout",
        "aplicar_cupon",
        "quitar_cupon",
        "preparar_reclamo",
        "listar_reclamos",
        "consultar_reclamo",
        "preparar_devolucion",
        "listar_devoluciones",
        "consultar_devolucion",
        "cerrar_sesion",
        "verificar_celular",
    ):
        assert tools[name]["requiere_sesion"] is True
    for name in ("buscar_productos", "nueva_conversacion", "buscar_conversaciones", "solicitar_login"):
        assert tools[name]["requiere_sesion"] is False
    assert tools["vaciar_carrito"]["requiere_confirmacion"] is True
    assert tools["iniciar_checkout"]["requiere_confirmacion"] is False
    assert tools["preparar_reclamo"]["requiere_confirmacion"] is False
    assert all(tool["parametros"]["additionalProperties"] is False for tool in tools.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["listar_pedidos", "consultar_pedido"])
async def test_retired_order_names_are_unknown_and_have_no_alias(name):
    handlers = {
        contract.name: AsyncMock()
        for contract in TOOL_CONTRACTS
        if contract.name != "solicitar_registro"
    }
    registry = build_tool_registry(handlers)
    result = await registry.ejecutar(name, {}, CONTEXT)
    assert result.code == "HERRAMIENTA_DESCONOCIDA"
    assert all(handler.await_count == 0 for handler in handlers.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("action", CLIENT_ACTIONS)
async def test_client_actions_never_reach_the_dispatcher(action):
    registry = build_tool_registry({"ver_carrito": AsyncMock(return_value=ToolResult("OK"))})
    result = await ActionDispatcher(registry).despachar(
        {"tipo": action, "payload": {}}, CONTEXT)
    assert result.code == "ACCION_DESCONOCIDA"


@pytest.mark.asyncio
async def test_confirmation_action_is_hidden_from_the_llm_and_needs_the_ui_click():
    handler = AsyncMock(return_value=ToolResult("OK", {"texto": "constancia"}))
    registry = ToolRegistry()
    registry.registrar(ToolDefinition(
        "confirmar_reclamo", "UI-only", ClaimConfirmArguments, handler,
        requiere_sesion=True, requiere_confirmacion=True, solo_ui=True,
    ))
    assert registry.catalogo() == []
    assert (await registry.ejecutar(
        "confirmar_reclamo", CLAIM_PAYLOAD, CONTEXT)).code == "HERRAMIENTA_DESCONOCIDA"
    dispatcher = ActionDispatcher(registry)
    pending = await dispatcher.despachar(
        {"tipo": "CONFIRMAR_RECLAMO", "payload": CLAIM_PAYLOAD}, CONTEXT)
    assert pending.code == "REQUIERE_CONFIRMACION"
    handler.assert_not_awaited()
    result = await dispatcher.despachar(
        {"tipo": "CONFIRMAR_RECLAMO", "payload": CLAIM_PAYLOAD},
        CONTEXT, confirmacion_ui=True,
    )
    assert result.code == "OK"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_checkout_without_session_keeps_the_pending_action():
    handler = AsyncMock(return_value=ToolResult("OK"))
    registry = build_tool_registry({"iniciar_checkout": handler})
    tool = next(t for t in registry.catalogo() if t["nombre"] == "iniciar_checkout")
    assert tool["requiere_sesion"] is True
    result = await registry.ejecutar("iniciar_checkout", {}, ANONYMOUS)
    assert result.code == "REQUIERE_SESION"
    assert result.data["accionPendiente"] == "INICIAR_CHECKOUT"
    handler.assert_not_awaited()
    assert (await registry.ejecutar("iniciar_checkout", {}, CONTEXT)).code == "OK"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_rejection_opens_the_login_form_with_the_pending_action():
    registry = build_tool_registry({"iniciar_checkout": AsyncMock(return_value=ToolResult("OK"))})
    adapter = RegistryToolAdapter(registry)
    result = await adapter.execute(
        "iniciar_checkout", {}, ToolExecutionContext(uuid.uuid4(), None, "sid-1"))
    assert result.code == "REQUIERE_SESION" and result.is_error
    (block,) = result.blocks
    assert block["tipo"] == "FORMULARIO" and block["formulario"] == "LOGIN"
    assert block["datos"]["accionPendiente"] == "INICIAR_CHECKOUT"


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "args"), [
    ("aplicar_cupon", {"codigo": "RUN10"}),
    ("quitar_cupon", {}),
    ("iniciar_checkout", {}),
    ("consultar_pedidos", {}),
    ("consultar_seguimiento", {"pedidoId": "PED-1"}),
    ("preparar_reclamo", {}),
    ("listar_reclamos", {}),
    ("consultar_reclamo", {"codigoSeguimiento": "REC-1"}),
    ("preparar_devolucion", {}),
    ("listar_devoluciones", {}),
    ("consultar_devolucion", {"devolucionId": "DEV-1"}),
    ("cerrar_sesion", {}),
    ("verificar_celular", {}),
])
async def test_session_tools_reject_anonymous_callers(name, args):
    handler = AsyncMock(return_value=ToolResult("OK"))
    registry = build_tool_registry({name: handler})
    result = await registry.ejecutar(name, args, ANONYMOUS)
    assert result.code == "REQUIERE_SESION"
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "args"), [
    ("aplicar_cupon", {"codigo": "RUN10", "descuento": 50}),
    ("aplicar_cupon", {"codigo": ""}),
    ("quitar_cupon", {"codigo": "RUN10"}),
    ("consultar_seguimiento", {}),
    ("consultar_seguimiento", {"pedidoId": "P-1", "pedidoRef": "P-1"}),
    ("buscar_conversaciones", {}),
    ("preparar_reclamo", {"motivo": "OTRO"}),
    ("preparar_reclamo", {"descripcion": "x" * 1001}),
    ("consultar_reclamo", {}),
    ("consultar_devolucion", {}),
    ("preparar_devolucion", {"tipo": "REEMBOLSO"}),
    ("confirmar_reclamo", {"pedidoId": "PED-1"}),
])
async def test_invalid_arguments_are_rejected_before_the_handler(name, args):
    handler = AsyncMock(return_value=ToolResult("OK"))
    if name.startswith("confirmar"):
        registry = ToolRegistry()
        registry.registrar(ToolDefinition(
            name, "UI-only", ClaimConfirmArguments, handler, solo_ui=True,
        ))
        result = await registry.ejecutar_accion(name, args, CONTEXT, confirmacion_ui=True)
    else:
        registry = build_tool_registry({name: handler})
        result = await registry.ejecutar(name, args, CONTEXT)
    assert result.code == "ARGUMENTOS_INVALIDOS"
    handler.assert_not_awaited()


def test_claim_confirmation_schema_forbids_extra_arguments():
    with pytest.raises(ValidationError):
        ClaimConfirmArguments.model_validate({**CLAIM_PAYLOAD, "confirmado": True})
    with pytest.raises(ValidationError):
        ClaimConfirmArguments.model_validate({**CLAIM_PAYLOAD, "descripcion": "corta"})
