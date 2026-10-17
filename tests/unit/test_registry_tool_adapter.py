"""RegistryToolAdapter: ToolExecutorPort over the ToolRegistry (SPEC-05 Req. 3 and 6)."""
import uuid
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, ConfigDict

from src.adapters.outbound.tools.registry_tool_adapter import RegistryToolAdapter
from src.application.tools.catalog_tools import build_tool_registry
from src.application.tools.tool_registry import (
    ExecutionContext, ToolDefinition, ToolRegistry, ToolResult as RegistryResult,
)
from src.infrastructure.config.container import provide_tool_executor
from src.ports.outbound.tool_executor_port import ToolExecutionContext, ToolExecutorPort

CONVERSATION = uuid.uuid4()
CUSTOMER = uuid.uuid4()
ANA = ToolExecutionContext(CONVERSATION, CUSTOMER, None)
ANONYMOUS = ToolExecutionContext(CONVERSATION, None, "sid-1")


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def adapter_with(**handlers):
    return RegistryToolAdapter(build_tool_registry(handlers))


def test_definitions_map_the_catalog_to_llm_tool_definitions():
    adapter = adapter_with(ver_carrito=AsyncMock())
    definitions = {d.name: d for d in adapter.definitions()}
    assert set(definitions) == {"solicitar_registro", "ver_carrito"}
    assert definitions["ver_carrito"].description.startswith("Ver el carrito")
    assert definitions["ver_carrito"].parameters["additionalProperties"] is False


def test_default_wiring_exposes_registration_plus_commercial_tools():
    executor: ToolExecutorPort = provide_tool_executor(object())
    assert [d.name for d in executor.definitions()] == [
        "solicitar_registro",
        "buscar_productos",
        "ver_detalle_producto",
        "consultar_disponibilidad",
        "agregar_al_carrito",
        "ver_carrito",
        "cambiar_cantidad",
        "quitar_del_carrito",
        "vaciar_carrito",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context, expected",
    [
        (ANA, ExecutionContext(str(CONVERSATION), str(CUSTOMER), None)),
        (ANONYMOUS, ExecutionContext(str(CONVERSATION), None, "sid-1")),
    ],
)
async def test_execute_converts_the_context_for_the_handler(context, expected):
    handler = AsyncMock(return_value=RegistryResult("OK"))
    await adapter_with(ver_carrito=handler).execute("ver_carrito", {}, context)
    assert handler.await_args.args[1] == expected


@pytest.mark.asyncio
async def test_handler_result_passes_through_as_non_error():
    handler = AsyncMock(return_value=RegistryResult("SIN_RESULTADOS", {"total": 0}))
    result = await adapter_with(ver_carrito=handler).execute("ver_carrito", {}, ANA)
    assert (result.code, dict(result.data), result.is_error, result.blocks) == ("SIN_RESULTADOS", {"total": 0}, False, ())


@pytest.mark.asyncio
async def test_blocks_are_extracted_and_stripped_from_the_data_for_the_model():
    block = {"tipo": "CARRUSEL_PRODUCTOS", "items": []}
    handler = AsyncMock(return_value=RegistryResult("OK", {"total": 2, "bloques": [block, "ignored"]}))
    result = await adapter_with(ver_carrito=handler).execute("ver_carrito", {}, ANA)
    assert [{k: v for k, v in b.items() if k != "id"} for b in result.blocks] == [block]
    assert dict(result.data) == {"total": 2}


@pytest.mark.asyncio
async def test_blocks_without_id_get_a_unique_one_and_existing_ids_are_kept():
    blocks = [{"tipo": "A"}, {"tipo": "B"}, {"id": "keep-me", "tipo": "C"}]
    handler = AsyncMock(return_value=RegistryResult("OK", {"bloques": blocks}))
    result = await adapter_with(ver_carrito=handler).execute("ver_carrito", {}, ANA)
    first, second, third = result.blocks
    assert uuid.UUID(first["id"]) != uuid.UUID(second["id"])
    assert third["id"] == "keep-me"
    assert "id" not in blocks[0]  # the handler's payload is not mutated


@pytest.mark.asyncio
async def test_registration_form_block_is_emitted_without_leaking_into_data():
    result = await adapter_with().execute("solicitar_registro", {}, ANONYMOUS)
    assert result.code == "OK" and not result.is_error
    (block,) = result.blocks
    assert uuid.UUID(block["id"])
    assert {k: v for k, v in block.items() if k != "id"} == {"tipo": "FORMULARIO", "formulario": "REGISTRO"}
    assert "bloques" not in result.data and result.data["texto"]


def test_registration_tool_description_restricts_it_to_account_creation():
    (registration,) = adapter_with().definitions()
    assert "solo cuando" in registration.description and "productos" in registration.description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name, arguments, context, code",
    [
        ("no_existe", {}, ANA, "HERRAMIENTA_DESCONOCIDA"),
        ("ver_carrito", {"extra": 1}, ANA, "ARGUMENTOS_INVALIDOS"),
        ("vaciar_carrito", {}, ANA, "REQUIERE_CONFIRMACION"),
    ],
)
async def test_registry_rejections_are_errors_and_never_reach_the_handler(name, arguments, context, code):
    handlers = {"ver_carrito": AsyncMock(), "vaciar_carrito": AsyncMock()}
    result = await adapter_with(**handlers).execute(name, arguments, context)
    assert result.code == code and result.is_error and result.blocks == ()
    assert all(h.await_count == 0 for h in handlers.values())


@pytest.mark.asyncio
async def test_session_required_rejection_is_an_error():
    registry = ToolRegistry()
    registry.registrar(ToolDefinition("cerrar_sesion", "x", _NoArgs, AsyncMock(), requiere_sesion=True))
    result = await RegistryToolAdapter(registry).execute("cerrar_sesion", {}, ANONYMOUS)
    assert result.code == "REQUIERE_SESION" and result.is_error


@pytest.mark.asyncio
async def test_handler_exceptions_propagate_to_the_agent_loop():
    handler = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await adapter_with(ver_carrito=handler).execute("ver_carrito", {}, ANA)
