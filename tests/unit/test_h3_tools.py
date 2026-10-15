"""SPEC-05: ejecución segura, suplantación, acciones UI; SPEC-01/06/09/10/11."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from src.application.tools.action_dispatcher import ActionDispatcher
from src.application.tools.h3_tools import EmptyArguments, QuantityArguments, crear_registro_h3
from src.application.tools.tool_registry import (
    ExecutionContext, ToolDefinition, ToolRegistry, ToolResult,
)


NAMES = (
    "buscar_productos", "ver_detalle_producto", "consultar_disponibilidad",
    "agregar_al_carrito", "ver_carrito", "cambiar_cantidad", "quitar_del_carrito", "vaciar_carrito",
)


@pytest.fixture
def tools():
    handlers = {name: AsyncMock(return_value=ToolResult("OK", {"desde": name})) for name in NAMES}
    return crear_registro_h3(handlers), handlers, ExecutionContext("conv-1", "cliente-real", "anon-1")


@pytest.mark.asyncio
async def test_intencion_de_registro_muestra_formulario_sin_datos(tools):
    registry, _, context = tools
    result = await registry.ejecutar("solicitar_registro", {}, context)
    assert result.code == "OK"
    assert result.data["texto"] == "Puedes crear tu cuenta completando este formulario."
    assert result.data["bloques"] == [{"tipo": "FORMULARIO", "formulario": "REGISTRO"}]
    rejected = await registry.ejecutar("solicitar_registro", {"contrasena": "no-al-llm"}, context)
    assert rejected.code == "ARGUMENTOS_INVALIDOS"
    assert "no-al-llm" not in repr(rejected)


@pytest.mark.asyncio
async def test_busqueda_clara_filtros_y_decimal(tools):
    registry, handlers, context = tools
    result = await registry.ejecutar("buscar_productos", {
        "categoria": "zapatillas", "marca": "Nike", "uso": "running", "precioMax": 350,
    }, context)
    assert result.code == "OK"
    argumentos, recibida = handlers["buscar_productos"].await_args.args
    assert argumentos.precioMax == Decimal("350")
    assert argumentos.canal == "CHATBOT" and argumentos.estado == "ACTIVO"
    assert argumentos.tamanio == 10
    assert recibida is context


@pytest.mark.asyncio
async def test_suplantacion_ignora_identidad_y_usa_contexto(tools):
    registry, handlers, context = tools
    args = {"clienteId": "victima", "sub": "otro", "chat_sid": "ajeno"}
    result = await registry.ejecutar("ver_carrito", args, context)
    assert result.code == "OK"
    validated, received = handlers["ver_carrito"].await_args.args
    assert validated.model_dump() == {}
    assert received.cliente_id == "cliente-real"
    assert args["clienteId"] == "victima"  # no muta entrada


@pytest.mark.asyncio
async def test_protegida_sin_sesion_no_ejecuta_y_deja_pendiente():
    registry, handler = ToolRegistry(), AsyncMock(return_value=ToolResult("OK"))
    registry.registrar(ToolDefinition("listar_pedidos", "Pedidos", EmptyArguments, handler, True))
    result = await registry.ejecutar("listar_pedidos", {"clienteId": "victima"}, ExecutionContext())
    assert result == ToolResult("REQUIERE_SESION", {
        "accionPendiente": {"herramienta": "listar_pedidos", "argumentos": {}},
    })
    handler.assert_not_awaited()
    await registry.ejecutar("listar_pedidos", {}, ExecutionContext(cliente_id="real"))
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_vaciar_carrito_solo_confirmacion_ui_del_servidor(tools):
    registry, handlers, context = tools
    assert (await registry.ejecutar("vaciar_carrito", {}, context)).code == "REQUIERE_CONFIRMACION"
    dispatcher = ActionDispatcher(registry)
    accion = {"tipo": "VACIAR_CARRITO", "payload": {}}
    assert (await dispatcher.despachar(accion, context)).code == "REQUIERE_CONFIRMACION"
    assert (await dispatcher.despachar({**accion, "confirmada": True}, context)).code == "ACCION_INVALIDA"
    assert (await dispatcher.despachar({
        "tipo": "VACIAR_CARRITO", "payload": {"confirmacion_ui": True},
    }, context, confirmacion_ui=True)).code == "ARGUMENTOS_INVALIDOS"
    handlers["vaciar_carrito"].assert_not_awaited()
    assert (await dispatcher.despachar(accion, context, confirmacion_ui=True)).code == "OK"
    handlers["vaciar_carrito"].assert_awaited_once()
    # Una confirmación UI anterior no habilita la próxima llamada del LLM.
    assert (await registry.ejecutar("vaciar_carrito", {}, context)).code == "REQUIERE_CONFIRMACION"
    assert handlers["vaciar_carrito"].await_count == 1


@pytest.mark.asyncio
async def test_agregar_ui_y_llm_mismo_handler_sin_ws(tools):
    registry, handlers, context = tools
    args = {"sku": "SKU-A", "cantidad": 1}
    assert (await registry.ejecutar("agregar_al_carrito", args, context)).code == "OK"
    result = await ActionDispatcher(registry).despachar({"tipo": "AGREGAR_AL_CARRITO", "payload": args}, context)
    assert result.code == "OK" and handlers["agregar_al_carrito"].await_count == 2
    calls = handlers["agregar_al_carrito"].await_args_list
    assert calls[0].args == calls[1].args


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("cambiar_cantidad", {"itemId": "x", "cantidad": -3}),
    ("cambiar_cantidad", {"itemId": "x", "cantidad": 11}),
    ("cambiar_cantidad", {"itemId": "x", "cantidad": True}),
    ("cambiar_cantidad", {"itemId": "x", "cantidad": "3"}),
    ("cambiar_cantidad", {"cantidad": 3}),
    ("agregar_al_carrito", {"sku": "A", "cantidad": 0}),
    ("agregar_al_carrito", {"sku": "A", "productoRef": 2}),
    ("ver_detalle_producto", {"productoRef": 0}),
    ("ver_detalle_producto", {"productoRef": 11}),
    ("ver_detalle_producto", {}),
    ("consultar_disponibilidad", {"product_id": "A", "productoRef": 1}),
    ("buscar_productos", {"precioMin": 101, "precioMax": 100}),
    ("buscar_productos", {"precioMax": -1}),
    ("buscar_productos", {"tamanio": 11}),
    ("buscar_productos", {"pagina": 0}),
    ("buscar_productos", {"estado": "BORRADOR"}),
    ("buscar_productos", {"canal": "RETAIL"}),
    ("quitar_del_carrito", {"itemRef": 21}),
])
async def test_argumentos_invalidos_sin_ejecutar(name, args, tools):
    registry, handlers, context = tools
    result = await registry.ejecutar(name, args, context)
    assert result.code == "ARGUMENTOS_INVALIDOS"
    handlers[name].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("ver_detalle_producto", {"productoRef": 3}),
    ("ver_detalle_producto", {"product_id": "P-1"}),
    ("consultar_disponibilidad", {"productoRef": 1, "talla": "40"}),
    ("agregar_al_carrito", {"productoRef": 1, "talla": "41"}),
    ("cambiar_cantidad", {"itemId": "x", "cantidad": 0}),
    ("cambiar_cantidad", {"nombre": "medias", "cantidad": 3}),
    ("quitar_del_carrito", {"itemRef": 2}),
])
async def test_referencias_y_cantidad_cero_para_caso_de_uso(name, args, tools):
    registry, handlers, context = tools
    assert (await registry.ejecutar(name, args, context)).code == "OK"
    validated, received = handlers[name].await_args.args
    assert validated.model_dump(exclude_unset=True) == args
    assert received is context


@pytest.mark.asyncio
async def test_json_malformado_y_errores_no_filtran_secretos(tools):
    registry, handlers, context = tools
    for args in ('{"cantidad":', '[]', 'null', '123', '{"itemId":"x","cantidad":"secreto"}'):
        result = await registry.ejecutar("cambiar_cantidad", args, context)
        assert result.code == "ARGUMENTOS_INVALIDOS"
        assert "secreto" not in repr(result)
    handlers["cambiar_cantidad"].assert_not_awaited()
    assert (await registry.ejecutar("ver_carrito", '{}', context)).code == "OK"
    assert (await registry.ejecutar("no_existe", {}, context)).code == "HERRAMIENTA_DESCONOCIDA"


@pytest.mark.asyncio
async def test_error_handler_no_reintenta_mutacion(tools):
    registry, handlers, context = tools
    handlers["agregar_al_carrito"].side_effect = RuntimeError("fallo transacción")
    with pytest.raises(RuntimeError, match="transacción"):
        await registry.ejecutar("agregar_al_carrito", {"sku": "A"}, context)
    handlers["agregar_al_carrito"].assert_awaited_once()


def test_catalogo_no_comparte_estado_y_registro_rechaza_duplicados(tools):
    registry, _, _ = tools
    catalog = registry.catalogo()
    assert len(catalog) == 9
    assert {t["nombre"] for t in catalog} == set(NAMES) | {"solicitar_registro"}
    assert next(t for t in catalog if t["nombre"] == "vaciar_carrito")["requiere_confirmacion"]
    assert all(t["parametros"]["additionalProperties"] is False for t in catalog)
    catalog[0]["nombre"] = "cambiado"
    assert registry.catalogo()[0]["nombre"] == "solicitar_registro"
    with pytest.raises(ValueError, match="duplicada"):
        registry.registrar(ToolDefinition("ver_carrito", "x", EmptyArguments, AsyncMock()))
    with pytest.raises(ValueError, match="extra"):
        registry.registrar(ToolDefinition("sin_esquema", "x", BaseModel, AsyncMock()))
    with pytest.raises(ValueError, match="ocho"):
        crear_registro_h3({})


@pytest.mark.asyncio
@pytest.mark.parametrize("accion", [{"tipo": "NO_EXISTE"}, {"tipo": []}, {"tipo": "VER_CARRITO", "payload": []}])
async def test_acciones_invalidas_no_llaman_handlers(accion, tools):
    registry, handlers, context = tools
    result = await ActionDispatcher(registry).despachar(accion, context)
    assert result.code in {"ACCION_INVALIDA", "ACCION_DESCONOCIDA"}
    assert all(h.await_count == 0 for h in handlers.values())
