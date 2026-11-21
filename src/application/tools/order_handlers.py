"""Chat order status (SPEC-17 Req. 1, 2 and 3): list the customer's orders or show one.

The handler forwards the customer's holder token to Ventas (`GET /pedidos/me` and
`GET /pedidos/{id}`), so the module filters by `sub` and a foreign order looks
exactly like an unknown one. Blocks follow the frontend contract:
`LISTA_PEDIDOS` carries `pedidos` and `ESTADO_PEDIDO` carries `hitos`.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from src.application.tools.catalog_tools import OrderQueryArguments
from src.application.use_cases.notification_service import NotificationError
from src.domain.services.status_mapper import SALES_STATUS_LABELS, map_order_status_label
from src.shared.application.tools.tool_context import caller_from_context
from src.shared.application.tools.tool_registry import ExecutionContext, ToolResult
from src.shared.domain.module_errors import ModuleRequestError, ModuleUnavailableError

LIMA = timezone(timedelta(hours=-5))
ORDER_FLOW = ("CREADO", "PAGADO", "EN_PREPARACION", "DESPACHADO", "ENTREGADO")
MAX_LISTED = 5
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


def _block(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"id": uuid.uuid4().hex, "tipo": kind, "datos": data}


def _actions(*actions: dict[str, Any]) -> dict[str, Any]:
    return _block("ACCIONES_RAPIDAS", {"acciones": list(actions)})


def _date(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(LIMA).strftime("%d/%m/%Y %H:%M")


def _money(total: Any, currency: Any) -> str:
    try:
        amount = Decimal(str(total))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    symbol = "S/" if str(currency or "PEN").upper() == "PEN" else str(currency)
    return f"{symbol} {amount:.2f}"


def _hitos(detail: dict[str, Any]) -> list[dict[str, Any]]:
    history = [row for row in detail.get("historial") or [] if isinstance(row, dict)]
    dates = {
        str(row.get("estado") or "").upper(): _date(row.get("fecha"))
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
        "fecha": _date(row.get("fechaCreacion")),
        "total": _money(row.get("total"), row.get("moneda")),
        "estado": status,
        "estadoEtiqueta": row.get("estadoEtiqueta") or map_order_status_label(status),
        "canal": row.get("canal"),
    }


def _detail_block(detail: dict[str, Any]) -> dict[str, Any]:
    summary = _summary(detail)
    return _block("ESTADO_PEDIDO", {
        "pedido": {**summary, "envio": detail.get("envio"), "items": detail.get("items") or []},
        "hitos": _hitos(detail),
    })


def _detail_text(detail: dict[str, Any]) -> str:
    summary = _summary(detail)
    if summary["estado"] == "ANULADO":
        return (f"Tu pedido {summary['pedidoId']} fue anulado. "
                "Si corresponde un reembolso, se procesará al medio de pago original.")
    if summary["estado"] == "ENTREGADO":
        return f"Tu pedido {summary['pedidoId']} ya fue entregado. ¿Todo bien con tu compra?"
    return (f"Tu pedido {summary['pedidoId']} está en estado: "
            f"{summary['estadoEtiqueta']}.")


def build_order_handlers(orders: OrdersQueryPort) -> dict[str, Any]:
    async def consultar_pedidos(args: OrderQueryArguments, context: ExecutionContext) -> ToolResult:
        caller = caller_from_context(context)
        if caller is None or caller.customer_id is None or not context.access_token:
            return ToolResult("REQUIERE_SESION", {
                "texto": "Inicia sesión para consultar tus pedidos.",
                "accionPendiente": {"herramienta": "consultar_pedidos",
                                    "argumentos": args.model_dump(mode="json")},
            })
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
                estado=args.estado, page=1, page_size=10,
            )
            items = [row for row in page.get("items") or [] if isinstance(row, dict)]
            if not items:
                return ToolResult("OK", {
                    "texto": "Aún no tienes pedidos. ¿Quieres ver las ofertas?",
                    "bloques": [_actions(
                        {"id": "VER_OFERTAS", "etiqueta": "Ver ofertas", "payload": {}})],
                })
            if len(items) == 1 and not args.estado:
                pedido_id = items[0].get("pedidoId")
                if isinstance(pedido_id, str) and pedido_id.strip():
                    detail = await orders.get_order_detail(
                        caller.customer_id, pedido_id,
                        holder_token=context.access_token,
                    )
                    return ToolResult("OK", {
                        "texto": _detail_text(detail),
                        "bloques": [_detail_block(detail)],
                    })
            summaries = [_summary(row) for row in items[:MAX_LISTED]]
            return ToolResult("OK", {
                "texto": "Estos son tus pedidos. Dime cuál quieres ver en detalle.",
                "bloques": [_block("LISTA_PEDIDOS", {
                    "pedidos": summaries,
                    "mensaje": "Puedes ver el historial completo en Mis pedidos.",
                })],
            })
        except ModuleRequestError as exc:
            if exc.status == 401:
                return ToolResult("REQUIERE_SESION", {
                    "texto": "Inicia sesión para consultar tus pedidos.",
                    "accionPendiente": {"herramienta": "consultar_pedidos",
                                        "argumentos": args.model_dump(mode="json")},
                })
            if exc.status in (403, 404):
                return ToolResult("OK", {
                    "texto": NOT_FOUND_TEXT,
                    "bloques": [_actions(
                        {"id": "CONSULTAR_PEDIDOS", "etiqueta": "Ver mis pedidos", "payload": {}})],
                })
            return ToolResult("OK", {
                "texto": UNAVAILABLE_TEXT,
                "bloques": [_actions(
                    {"id": "CONSULTAR_PEDIDOS", "etiqueta": "Reintentar", "payload": {}})],
            })
        except ModuleUnavailableError:
            return ToolResult("OK", {
                "texto": UNAVAILABLE_TEXT,
                "bloques": [_actions(
                    {"id": "CONSULTAR_PEDIDOS", "etiqueta": "Reintentar", "payload": {}})],
            })
        except NotificationError:
            return ToolResult("OK", {
                "texto": UNAVAILABLE_TEXT,
                "bloques": [_actions(
                    {"id": "CONSULTAR_PEDIDOS", "etiqueta": "Reintentar", "payload": {}})],
            })

    return {"consultar_pedidos": consultar_pedidos}
