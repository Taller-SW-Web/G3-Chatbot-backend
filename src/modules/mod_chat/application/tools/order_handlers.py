"""Chat order status and tracking (SPEC-17 Req. 1, 2 and 3; SPEC-18).

The handlers forward the customer's holder token to Ventas (`GET /pedidos/me`,
`GET /pedidos/{id}` and the tracking service), so the module filters by `sub`
and a foreign order looks exactly like an unknown one. Blocks follow the
frontend contract: `LISTA_PEDIDOS` carries `pedidos`; `ESTADO_PEDIDO` carries
`hitos` from Ventas and, for tracking, `datos.seguimiento` from Despacho.
"""
from typing import Any, Protocol

from src.modules.mod_catalog.public import OrderQueryArguments
from src.modules.mod_chat.application.tools.tool_blocks import (
    actions,
    block,
    format_date,
    format_money,
)
from src.modules.mod_orders.public import (
    SALES_STATUS_LABELS,
    NotificationError,
    map_order_status_label,
    sanitize_tracking_payload,
)
from src.shared.application.tools.tool_context import caller_from_context
from src.shared.application.tools.tool_registry import ExecutionContext, ToolResult
from src.shared.domain.module_errors import ModuleRequestError, ModuleUnavailableError

ORDER_FLOW = ("CREADO", "PAGADO", "EN_PREPARACION", "DESPACHADO", "ENTREGADO")
NON_FINAL_STATES = frozenset({"CREADO", "PAGADO", "EN_PREPARACION", "DESPACHADO"})
MAX_LISTED = 5
QUERY_PAGE_SIZE = 50
NOT_FOUND_TEXT = "No encontré ese pedido en tu cuenta. Revisa el número e inténtalo de nuevo."
UNAVAILABLE_TEXT = "No puedo consultar tus pedidos en este momento. ¿Quieres reintentar?"


class OrdersQueryPort(Protocol):
    async def list_orders(
        self, customer_id: Any, *, holder_token: str, estado: str | None = None,
        page: int = 1, page_size: int = 10,
    ) -> dict[str, Any]:
        ...

    async def get_order_detail(
        self, customer_id: Any, order_id: str, *, holder_token: str,
    ) -> dict[str, Any]:
        ...

    async def get_order_tracking(
        self, customer_id: Any, order_id: str, *, holder_token: str,
    ) -> dict[str, Any]:
        ...


def _hitos(detail: dict[str, Any]) -> list[dict[str, Any]]:
    history = [row for row in detail.get("historial") or [] if isinstance(row, dict)]
    dates = {
        str(row.get("estado") or "").upper(): format_date(row.get("fecha"))
        for row in history if row.get("estado")
    }
    current = str(detail.get("estado") or "").upper()
    if current == "ANULADO":
        return [{"titulo": SALES_STATUS_LABELS["ANULADO"], "fecha": dates.get("ANULADO") or
                 dates.get("CREADO", ""), "done": True}]
    position = ORDER_FLOW.index(current) if current in ORDER_FLOW else -1
    hitos = [
        {
            "titulo": SALES_STATUS_LABELS[state],
            "fecha": dates.get(state, ""),
            "done": index <= position,
        }
        for index, state in enumerate(ORDER_FLOW)
    ]
    extra = [state for state in dates if state and state not in {*ORDER_FLOW, "ANULADO"}]
    hitos.extend({"titulo": map_order_status_label(state), "fecha": dates[state], "done": True}
                 for state in extra)
    return hitos


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("estado") or "").upper()
    return {
        "pedidoId": str(row.get("pedidoId") or ""),
        "fecha": format_date(row.get("fechaCreacion")),
        "total": format_money(row.get("total"), row.get("moneda")),
        "estado": status,
        "estadoEtiqueta": row.get("estadoEtiqueta") or map_order_status_label(status),
        "canal": row.get("canal"),
    }


def order_list_block(items: list[dict[str, Any]]) -> dict[str, Any]:
    """`LISTA_PEDIDOS` for order selection (SPEC-17 Req. 1)."""
    return block("LISTA_PEDIDOS", {
        "pedidos": [_summary(row) for row in items[:MAX_LISTED]],
        "mensaje": "Puedes ver el historial completo en Mis pedidos.",
    })


def order_unavailable(
    action_id: str = "CONSULTAR_PEDIDOS", payload: dict[str, Any] | None = None,
) -> ToolResult:
    return ToolResult("OK", {
        "texto": UNAVAILABLE_TEXT,
        "bloques": [actions(
            {"id": action_id, "etiqueta": "Reintentar", "payload": payload or {}})],
    })


def order_not_found() -> ToolResult:
    return ToolResult("OK", {
        "texto": NOT_FOUND_TEXT,
        "bloques": [actions(
            {"id": "CONSULTAR_PEDIDOS", "etiqueta": "Ver mis pedidos", "payload": {}})],
    })


def _detail_block(detail: dict[str, Any], seguimiento: dict[str, Any] | None = None) -> dict[str, Any]:
    summary = _summary(detail)
    data: dict[str, Any] = {
        "pedido": {**summary, "envio": detail.get("envio"), "items": detail.get("items") or []},
        "hitos": _hitos(detail),
    }
    if seguimiento is not None:
        data["seguimiento"] = seguimiento
    return block("ESTADO_PEDIDO", data)


def _detail_text(detail: dict[str, Any]) -> str:
    summary = _summary(detail)
    if summary["estado"] == "ANULADO":
        return (f"Tu pedido {summary['pedidoId']} fue anulado. "
                "Si corresponde un reembolso, se procesará al medio de pago original.")
    if summary["estado"] == "ENTREGADO":
        return f"Tu pedido {summary['pedidoId']} ya fue entregado. ¿Todo bien con tu compra?"
    return (f"Tu pedido {summary['pedidoId']} está en estado: "
            f"{summary['estadoEtiqueta']}.")


def _tracking_text(detail: dict[str, Any], seguimiento: dict[str, Any]) -> str:
    summary = _summary(detail)
    if not seguimiento.get("detalleDisponible"):
        return str(seguimiento.get("mensaje") or _detail_text(detail))
    etiqueta = str(seguimiento.get("estadoEtiqueta") or summary["estadoEtiqueta"]).strip()
    parts = [f"Tu pedido {summary['pedidoId']} está en estado: {etiqueta}."]
    if seguimiento.get("fechaProgramada"):
        parts.append(f"Fecha programada: {seguimiento['fechaProgramada']}.")
    if seguimiento.get("distrito"):
        parts.append(f"Distrito: {seguimiento['distrito']}.")
    return " ".join(parts)


def _requiere_sesion(herramienta: str, args: Any) -> ToolResult:
    return ToolResult("REQUIERE_SESION", {
        "texto": "Inicia sesión para consultar tus pedidos.",
        "accionPendiente": {"herramienta": herramienta,
                            "argumentos": args.model_dump(mode="json")},
    })


def build_order_handlers(orders: OrdersQueryPort) -> dict[str, Any]:
    async def consultar_pedidos(args: OrderQueryArguments, context: ExecutionContext) -> ToolResult:
        caller = caller_from_context(context)
        if caller is None or caller.customer_id is None or not context.access_token:
            return _requiere_sesion("consultar_pedidos", args)
        try:
            if args.pedidoId:
                detail = await orders.get_order_detail(
                    caller.customer_id, args.pedidoId, holder_token=context.access_token,
                )
                return ToolResult("OK", {
                    "texto": _detail_text(detail),
                    "bloques": [_detail_block(detail)],
                })
            page = await orders.list_orders(
                caller.customer_id, holder_token=context.access_token,
                estado=args.estado, page=1, page_size=QUERY_PAGE_SIZE,
            )
            items = [row for row in page.get("items") or [] if isinstance(row, dict)]
            if not items:
                return ToolResult("OK", {
                    "texto": "Aún no tienes pedidos. ¿Quieres ver las ofertas?",
                    "bloques": [actions(
                        {"id": "VER_OFERTAS", "etiqueta": "Ver ofertas", "payload": {}})],
                })
            # D7 / SPEC-17 Req. 1: show the detail only when exactly one order is
            # still in progress. A lone delivered or cancelled order stays a list.
            en_curso = [
                row for row in items
                if str(row.get("estado") or "").upper() in NON_FINAL_STATES
            ]
            if len(en_curso) == 1 and not args.estado:
                pedido_id = en_curso[0].get("pedidoId")
                if isinstance(pedido_id, str) and pedido_id.strip():
                    detail = await orders.get_order_detail(
                        caller.customer_id, pedido_id,
                        holder_token=context.access_token,
                    )
                    return ToolResult("OK", {
                        "texto": _detail_text(detail),
                        "bloques": [_detail_block(detail)],
                    })
            return ToolResult("OK", {
                "texto": "Estos son tus pedidos. Dime cuál quieres ver en detalle.",
                "bloques": [order_list_block(items)],
            })
        except ModuleRequestError as exc:
            if exc.status == 401:
                return _requiere_sesion("consultar_pedidos", args)
            if exc.status in (403, 404):
                return order_not_found()
            return order_unavailable()
        except (ModuleUnavailableError, NotificationError):
            return order_unavailable()

    async def consultar_seguimiento(args: Any, context: ExecutionContext) -> ToolResult:
        caller = caller_from_context(context)
        if caller is None or caller.customer_id is None or not context.access_token:
            return _requiere_sesion("consultar_seguimiento", args)
        order_id = str(args.pedidoId or args.pedidoRef or "").strip()
        try:
            detail = await orders.get_order_detail(
                caller.customer_id, order_id, holder_token=context.access_token,
            )
            tracking = await orders.get_order_tracking(
                caller.customer_id, order_id, holder_token=context.access_token,
            )
        except ModuleRequestError as exc:
            if exc.status == 401:
                return _requiere_sesion("consultar_seguimiento", args)
            if exc.status in (403, 404):
                return order_not_found()
            return order_unavailable(
                "CONSULTAR_SEGUIMIENTO", {"pedidoId": order_id})
        except (ModuleUnavailableError, NotificationError):
            return order_unavailable("CONSULTAR_SEGUIMIENTO", {"pedidoId": order_id})
        # SPEC-18 Req. 3: the UI gets the whitelist including `recibidoPor`; the
        # registry adapter strips blocks before the model sees anything.
        seguimiento = sanitize_tracking_payload(tracking, pedido_id=order_id, for_llm=False)
        return ToolResult("OK", {
            "texto": _tracking_text(detail, seguimiento),
            "bloques": [_detail_block(detail, seguimiento)],
        })

    return {
        "consultar_pedidos": consultar_pedidos,
        "consultar_seguimiento": consultar_seguimiento,
    }
