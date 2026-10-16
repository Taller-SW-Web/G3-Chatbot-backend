"""Commercial cart handlers for the tool registry (SPEC-11, SPEC-10).

Same handlers serve the LLM path and direct UI actions (SPEC-05 Req. 8):
the dispatcher calls them with validated arguments while the router
validates the explicit UI confirmation for `vaciar_carrito` beforehand.
"""
import uuid

from src.adapters.outbound.http_clients.base_client import ModuleUnavailableError
from src.application.tools.tool_context import (
    caller_from_context,
    conversation_id_from_context,
)
from src.application.tools.tool_registry import ExecutionContext, ToolResult
from src.application.use_cases.manage_cart_use_case import CartError


def build_cart_handlers(cart_use_case) -> dict:
    async def _run(operation, context: ExecutionContext) -> ToolResult:
        caller = caller_from_context(context)
        conversation_id = conversation_id_from_context(context)
        if caller is None or conversation_id is None:
            return ToolResult("RECURSO_NO_ENCONTRADO", {"texto": ""})
        try:
            result = await operation(caller, conversation_id)
        except CartError as exc:
            data: dict = {"texto": exc.detail}
            data.update(exc.data)
            return ToolResult(exc.code, data)
        except ModuleUnavailableError as exc:
            return ToolResult("SERVICIO_NO_DISPONIBLE", {"modulo": exc.module})
        return ToolResult(result["code"], {
            "texto": result["texto"], "bloques": result["bloques"],
        })

    async def agregar_al_carrito(args, context: ExecutionContext) -> ToolResult:
        return await _run(
            lambda caller, conversation_id: cart_use_case.add(
                caller, conversation_id, sku=args.sku,
                producto_ref=args.productoRef, product_id=args.product_id,
                size=args.talla, color=args.color, quantity=args.cantidad,
            ),
            context,
        )

    async def ver_carrito(args, context: ExecutionContext) -> ToolResult:
        return await _run(cart_use_case.view, context)

    async def cambiar_cantidad(args, context: ExecutionContext) -> ToolResult:
        return await _run(
            lambda caller, conversation_id: cart_use_case.update_quantity(
                caller, conversation_id, item_id=args.itemId,
                item_ref=args.itemRef, name=args.nombre, quantity=args.cantidad,
            ),
            context,
        )

    async def quitar_del_carrito(args, context: ExecutionContext) -> ToolResult:
        return await _run(
            lambda caller, conversation_id: cart_use_case.remove_item(
                caller, conversation_id, item_id=args.itemId,
                item_ref=args.itemRef, name=args.nombre,
            ),
            context,
        )

    async def vaciar_carrito(args, context: ExecutionContext) -> ToolResult:
        return await _run(cart_use_case.clear, context)

    return {
        "agregar_al_carrito": agregar_al_carrito,
        "ver_carrito": ver_carrito,
        "cambiar_cantidad": cambiar_cantidad,
        "quitar_del_carrito": quitar_del_carrito,
        "vaciar_carrito": vaciar_carrito,
    }
