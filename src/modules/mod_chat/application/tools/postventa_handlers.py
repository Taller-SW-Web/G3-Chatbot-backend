"""Post-sale chat tools (SPEC-19..22) over `mod-postventa` and `mod-orders`.

Reads reuse the module services with the customer's holder token, so Ventas
filters by `sub` and a foreign claim/return looks like an unknown one. The
`preparar_*` tools only build a form; registering happens exclusively through
the UI-only `CONFIRMAR_RECLAMO`/`CONFIRMAR_DEVOLUCION` actions.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from src.modules.mod_catalog.public import ClaimConfirmArguments, ReturnConfirmArguments
from src.modules.mod_chat.application.tools.order_handlers import (
    order_list_block,
    order_not_found,
    order_unavailable,
)
from src.modules.mod_chat.application.tools.tool_blocks import actions, block, form, format_date
from src.modules.mod_postventa.public import (
    OPEN_CLAIM_STATES,
    PostventaError,
    PostventaService,
    map_claim_status_label,
    map_return_status_label,
)
from src.shared.application.tools.tool_context import (
    caller_from_context,
    conversation_id_from_context,
)
from src.shared.application.tools.tool_registry import (
    ExecutionContext,
    ToolDefinition,
    ToolResult,
)
from src.shared.domain.module_errors import ModuleRequestError, ModuleUnavailableError

CLAIM_UNAVAILABLE = "No puedo consultar tus reclamos ahora. ¿Quieres reintentar?"
RETURN_UNAVAILABLE = "No puedo consultar tus devoluciones ahora. ¿Quieres reintentar?"
CLAIM_NOT_FOUND = "No encontré ese reclamo en tu cuenta."
RETURN_NOT_FOUND = "No encontré esa solicitud en tu cuenta."
CLAIM_CONSTANCIA_TEXT = "Guarda este código para consultar tu reclamo."
RETURN_CONSTANCIA_TEXT = "Te avisaremos cuando el equipo de ventas revise tu solicitud."
RETURN_WINDOW_DAYS = 7
PAGE_SIZE = 10


class OrdersPort(Protocol):
    async def list_orders(
        self, customer_id: Any, *, holder_token: str, estado: str | None = None,
        page: int = 1, page_size: int = 10,
    ) -> dict[str, Any]:
        ...

    async def get_order_detail(
        self, customer_id: Any, order_id: str, *, holder_token: str,
    ) -> dict[str, Any]:
        ...


def _session_required(tool: str, args: Any) -> ToolResult:
    pending = {"herramienta": tool, "argumentos": args.model_dump(mode="json")}
    return ToolResult("REQUIERE_SESION", {
        "texto": "Inicia sesión para consultar tu cuenta.",
        "accionPendiente": pending,
        "bloques": [form("LOGIN", {"accionPendiente": pending})],
    })


def _claim_summary(item: dict[str, Any]) -> dict[str, Any]:
    estado = str(item.get("estado") or "").upper()
    return {
        "codigoSeguimiento": str(item.get("codigoSeguimiento") or ""),
        "fecha": format_date(item.get("fechaRegistro")),
        "pedidoId": item.get("pedidoId"),
        "tipo": item.get("tipo"),
        "motivo": item.get("motivo"),
        "estado": estado,
        "estadoEtiqueta": map_claim_status_label(estado),
        "fechaLimiteSLA": item.get("fechaLimiteSLA"),
    }


def _claim_detail(item: dict[str, Any]) -> dict[str, Any]:
    data = _claim_summary(item)
    respuesta = item.get("respuestaVisibleCliente")
    if respuesta:
        data["respuestaVisibleCliente"] = respuesta
    return data


def _claim_constancia(item: dict[str, Any]) -> dict[str, Any]:
    data = _claim_summary(item)
    data["fechaLimiteSLA"] = data.get("fechaLimiteSLA") or ""
    data["mensaje"] = CLAIM_CONSTANCIA_TEXT
    return data


def _return_summary(item: dict[str, Any]) -> dict[str, Any]:
    estado = str(item.get("estado") or "").upper()
    data = {
        "devolucionId": str(item.get("devolucionId") or ""),
        "pedidoId": item.get("pedidoId"),
        "fecha": format_date(item.get("fechaRegistro")),
        "tipo": item.get("tipo"),
        "motivo": item.get("motivo"),
        "estado": estado,
        "estadoEtiqueta": map_return_status_label(estado),
    }
    if item.get("estadoReembolso"):
        data["estadoReembolso"] = item["estadoReembolso"]
    return data


def _return_detail(item: dict[str, Any]) -> dict[str, Any]:
    data = _return_summary(item)
    resolucion = item.get("resolucion")
    if isinstance(resolucion, dict):
        clean: dict[str, Any] = {}
        if resolucion.get("fundamento"):
            clean["fundamento"] = resolucion["fundamento"]
        reembolso = resolucion.get("reembolso")
        if isinstance(reembolso, dict) and reembolso:
            clean["reembolso"] = {
                key: reembolso.get(key)
                for key in ("reembolsoId", "estado", "monto", "moneda", "fechaEjecucion")
            }
        if clean:
            data["resolucion"] = clean
    return data


def _claims_block(items: list[dict[str, Any]]) -> dict[str, Any]:
    return block("LISTA_RECLAMOS", {"reclamos": [_claim_summary(item) for item in items]})


def _returns_block(items: list[dict[str, Any]]) -> dict[str, Any]:
    return block("LISTA_DEVOLUCIONES", {"devoluciones": [_return_summary(item) for item in items]})


def _claim_actions(code: str | None = None) -> dict[str, Any]:
    if code:
        return actions(
            {"id": "CONSULTAR_RECLAMO", "etiqueta": "Ver estado",
             "payload": {"codigoSeguimiento": code}},
        )
    return actions(
        {"id": "PREPARAR_RECLAMO", "etiqueta": "Reportar un problema con un pedido", "payload": {}},
    )


def _return_actions() -> dict[str, Any]:
    return actions(
        {"id": "PREPARAR_DEVOLUCION", "etiqueta": "Solicitar cambio o devolución", "payload": {}},
    )


def _duplicate_claim(exc: PostventaError) -> ToolResult:
    data = {"texto": exc.detail or "Ya existe un reclamo abierto para este pedido y motivo."}
    code = exc.extra.get("codigoSeguimiento")
    data.update(exc.extra)
    return ToolResult("RECLAMO_DUPLICADO", {
        **data,
        "bloques": [_claim_actions(str(code) if code else None)],
    })


def _duplicate_return(exc: PostventaError) -> ToolResult:
    data = {"texto": exc.detail or "Ya existe una solicitud abierta para este pedido."}
    data.update(exc.extra)
    result = ToolResult("DEVOLUCION_DUPLICADA", data)
    return_id = exc.extra.get("devolucionId")
    if not return_id:
        return result
    return ToolResult("DEVOLUCION_DUPLICADA", {
        **data,
        "bloques": [actions(
            {"id": "CONSULTAR_DEVOLUCION", "etiqueta": "Ver estado",
             "payload": {"devolucionId": return_id}},
        )],
    })


def _service_error(
    exc: Exception, *, tool: str, args: Any, unavailable: str, not_found: str,
) -> ToolResult:
    if isinstance(exc, PostventaError):
        data = {"texto": exc.detail or unavailable}
        data.update(exc.extra)
        return ToolResult(exc.code, data)
    if isinstance(exc, ModuleRequestError):
        if exc.status == 401:
            return _session_required(tool, args)
        if exc.status in (403, 404):
            return ToolResult("OK", {"texto": not_found})
        if exc.status in (400, 409, 422):
            return ToolResult(exc.code, {"texto": unavailable})
        return ToolResult("SERVICIO_NO_DISPONIBLE", {"texto": unavailable})
    if isinstance(exc, ModuleUnavailableError):
        return ToolResult("SERVICIO_NO_DISPONIBLE", {"texto": unavailable})
    raise exc


def _delivered_at(detail: dict[str, Any]) -> datetime | None:
    for row in detail.get("historial") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("estado") or "").upper() != "ENTREGADO":
            continue
        try:
            parsed = datetime.fromisoformat(str(row.get("fecha")).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _idempotency_key(action: str, context: ExecutionContext, args: Any) -> str:
    conversation_id = context.conversacion_id or ""
    payload = json.dumps(args.model_dump(mode="json"), sort_keys=True, default=str)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"chat:{action}:{conversation_id}:{payload}"))


def build_postventa_handlers(postventa: PostventaService, orders: OrdersPort) -> dict[str, Any]:
    async def _list_orders(context: ExecutionContext, args: Any) -> dict[str, Any] | ToolResult:
        token = context.access_token
        caller = caller_from_context(context)
        if not token or caller is None or caller.customer_id is None:
            return _session_required("consultar_pedidos", args)
        try:
            return await orders.list_orders(
                caller.customer_id, holder_token=token,
                page=1, page_size=PAGE_SIZE,
            )
        except ModuleRequestError as exc:
            if exc.status == 401:
                return _session_required("consultar_pedidos", args)
            if exc.status in (403, 404):
                return order_not_found()
            return order_unavailable()
        except ModuleUnavailableError:
            return order_unavailable()

    async def _order_detail(
        pedido_id: str, context: ExecutionContext, args: Any, tool: str,
    ) -> dict[str, Any] | ToolResult:
        token = context.access_token
        caller = caller_from_context(context)
        if not token or caller is None or caller.customer_id is None:
            return _session_required(tool, args)
        try:
            return await orders.get_order_detail(
                caller.customer_id, pedido_id, holder_token=token,
            )
        except ModuleRequestError as exc:
            if exc.status == 401:
                return _session_required(tool, args)
            if exc.status in (403, 404):
                return order_not_found()
            return order_unavailable()
        except ModuleUnavailableError:
            return order_unavailable()

    async def preparar_reclamo(args: Any, context: ExecutionContext) -> ToolResult:
        if not context.cliente_id or not context.access_token:
            return _session_required("preparar_reclamo", args)
        if not args.pedidoRef:
            page = await _list_orders(context, args)
            if isinstance(page, ToolResult):
                return page
            items = [row for row in page.get("items") or [] if isinstance(row, dict)]
            if not items:
                return ToolResult("OK", {
                    "texto": "Aún no tienes pedidos para reclamar. ¿Quieres ver las ofertas?",
                    "bloques": [actions(
                        {"id": "VER_OFERTAS", "etiqueta": "Ver ofertas", "payload": {}})],
                })
            return ToolResult("OK", {
                "texto": "Elige el pedido sobre el que quieres reclamar.",
                "bloques": [order_list_block(items)],
            })
        detail = await _order_detail(str(args.pedidoRef), context, args, "preparar_reclamo")
        if isinstance(detail, ToolResult):
            return detail
        data = {
            "pedidoId": str(detail.get("pedidoId") or args.pedidoRef),
            "tipo": args.tipo,
            "motivo": args.motivo,
            "descripcion": args.descripcion,
            "pedidoCliente": args.pedidoCliente,
            "pedido": {
                "pedidoId": detail.get("pedidoId"),
                "estado": str(detail.get("estado") or "").upper(),
                "estadoEtiqueta": detail.get("estadoEtiqueta"),
            },
        }
        return ToolResult("OK", {
            "texto": "Revisa los datos de tu reclamo y envíalo cuando estés listo.",
            "bloques": [form("RECLAMO", data)],
        })

    async def preparar_devolucion(args: Any, context: ExecutionContext) -> ToolResult:
        if not context.cliente_id or not context.access_token:
            return _session_required("preparar_devolucion", args)
        if not args.pedidoRef:
            page = await _list_orders(context, args)
            if isinstance(page, ToolResult):
                return page
            items = [row for row in page.get("items") or [] if isinstance(row, dict)]
            if not items:
                return ToolResult("OK", {
                    "texto": "Aún no tienes pedidos con entrega para solicitar un cambio o devolución.",
                })
            return ToolResult("OK", {
                "texto": "Elige el pedido entregado sobre el que quieres solicitar el cambio o devolución.",
                "bloques": [order_list_block(items)],
            })
        detail = await _order_detail(str(args.pedidoRef), context, args, "preparar_devolucion")
        if isinstance(detail, ToolResult):
            return detail
        pedido_id = str(detail.get("pedidoId") or args.pedidoRef)
        estado = str(detail.get("estado") or "").upper()
        if estado != "ENTREGADO":
            return ToolResult("OK", {
                "texto": (
                    "Solo puedes solicitar un cambio o devolución después de la entrega. "
                    "Si el problema es la demora, puedo ayudarte con un seguimiento o un reclamo."
                ),
                "bloques": [actions(
                    {"id": "CONSULTAR_SEGUIMIENTO", "etiqueta": "Ver seguimiento",
                     "payload": {"pedidoId": pedido_id}},
                    {"id": "PREPARAR_RECLAMO", "etiqueta": "Crear un reclamo",
                     "payload": {"pedidoRef": pedido_id}},
                )],
            })
        delivered_at = _delivered_at(detail)
        expired = (
            delivered_at is not None
            and datetime.now(timezone.utc) - delivered_at > timedelta(days=RETURN_WINDOW_DAYS)
        )
        if expired:
            return ToolResult("OK", {
                "texto": (
                    f"El plazo de {RETURN_WINDOW_DAYS} días para solicitar un cambio o devolución "
                    "de este pedido ya venció."
                ),
                "bloques": [actions(
                    {"id": "PREPARAR_RECLAMO", "etiqueta": "Crear un reclamo",
                     "payload": {"pedidoRef": pedido_id}},
                )],
            })
        data = {
            "pedidoId": pedido_id,
            "lineaRef": args.lineaRef,
            "tipo": args.tipo,
            "motivo": args.motivo,
            "varianteDeseada": args.varianteDeseada,
            "descripcion": args.descripcion,
            "items": detail.get("items") or [],
        }
        return ToolResult("OK", {
            "texto": "Revisa los datos de tu solicitud y envíala cuando estés listo.",
            "bloques": [form("DEVOLUCION", data)],
        })

    async def listar_reclamos(args: Any, context: ExecutionContext) -> ToolResult:
        caller = caller_from_context(context)
        if caller is None or caller.customer_id is None or not context.access_token:
            return _session_required("listar_reclamos", args)
        try:
            page = await postventa.list_claims(
                holder_token=context.access_token, page=1, page_size=PAGE_SIZE,
            )
        except (PostventaError, ModuleRequestError, ModuleUnavailableError) as exc:
            return _service_error(
                exc, tool="listar_reclamos", args=args,
                unavailable=CLAIM_UNAVAILABLE, not_found=CLAIM_NOT_FOUND,
            )
        items = [row for row in page.get("items") or [] if isinstance(row, dict)]
        if not items:
            return ToolResult("OK", {
                "texto": "No tienes reclamos registrados.",
                "bloques": [_claim_actions()],
            })
        open_items = [
            row for row in items
            if str(row.get("estado") or "").upper() in OPEN_CLAIM_STATES
        ]
        if len(open_items) == 1:
            code = str(open_items[0].get("codigoSeguimiento") or "")
            try:
                detail = await postventa.get_claim(code=code, holder_token=context.access_token)
            except (PostventaError, ModuleRequestError, ModuleUnavailableError) as exc:
                return _service_error(
                    exc, tool="consultar_reclamo", args=args,
                    unavailable=CLAIM_UNAVAILABLE, not_found=CLAIM_NOT_FOUND,
                )
            return ToolResult("OK", {
                "texto": "Este es el estado de tu reclamo.",
                "bloques": [block("ESTADO_RECLAMO", _claim_detail(detail))],
            })
        return ToolResult("OK", {
            "texto": "Estos son tus reclamos. Dime cuál quieres revisar.",
            "bloques": [_claims_block(items)],
        })

    async def consultar_reclamo(args: Any, context: ExecutionContext) -> ToolResult:
        caller = caller_from_context(context)
        if caller is None or caller.customer_id is None or not context.access_token:
            return _session_required("consultar_reclamo", args)
        try:
            detail = await postventa.get_claim(
                code=str(args.codigoSeguimiento), holder_token=context.access_token,
            )
        except (PostventaError, ModuleRequestError, ModuleUnavailableError) as exc:
            result = _service_error(
                exc, tool="consultar_reclamo", args=args,
                unavailable=CLAIM_UNAVAILABLE, not_found=CLAIM_NOT_FOUND,
            )
            if result.code == "OK":
                return ToolResult("OK", {
                    "texto": CLAIM_NOT_FOUND,
                    "bloques": [actions(
                        {"id": "MIS_RECLAMOS", "etiqueta": "Ver mis reclamos", "payload": {}})],
                })
            return result
        return ToolResult("OK", {
            "texto": f"Tu reclamo está en estado: {map_claim_status_label(detail.get('estado'))}.",
            "bloques": [block("ESTADO_RECLAMO", _claim_detail(detail))],
        })

    async def listar_devoluciones(args: Any, context: ExecutionContext) -> ToolResult:
        caller = caller_from_context(context)
        if caller is None or caller.customer_id is None or not context.access_token:
            return _session_required("listar_devoluciones", args)
        try:
            page = await postventa.list_returns(
                holder_token=context.access_token, page=1, page_size=PAGE_SIZE,
            )
        except (PostventaError, ModuleRequestError, ModuleUnavailableError) as exc:
            return _service_error(
                exc, tool="listar_devoluciones", args=args,
                unavailable=RETURN_UNAVAILABLE, not_found=RETURN_NOT_FOUND,
            )
        items = [row for row in page.get("items") or [] if isinstance(row, dict)]
        if not items:
            return ToolResult("OK", {
                "texto": "Aún no tienes solicitudes de cambio o devolución.",
                "bloques": [_return_actions()],
            })
        return ToolResult("OK", {
            "texto": "Estas son tus solicitudes de cambio o devolución.",
            "bloques": [_returns_block(items)],
        })

    async def consultar_devolucion(args: Any, context: ExecutionContext) -> ToolResult:
        caller = caller_from_context(context)
        if caller is None or caller.customer_id is None or not context.access_token:
            return _session_required("consultar_devolucion", args)
        try:
            detail = await postventa.get_return(
                return_id=str(args.devolucionId), holder_token=context.access_token,
            )
        except (PostventaError, ModuleRequestError, ModuleUnavailableError) as exc:
            result = _service_error(
                exc, tool="consultar_devolucion", args=args,
                unavailable=RETURN_UNAVAILABLE, not_found=RETURN_NOT_FOUND,
            )
            if result.code == "OK":
                return ToolResult("OK", {
                    "texto": RETURN_NOT_FOUND,
                    "bloques": [actions(
                        {"id": "MIS_DEVOLUCIONES", "etiqueta": "Ver mis devoluciones",
                         "payload": {}})],
                })
            return result
        return ToolResult("OK", {
            "texto": f"Tu solicitud está en estado: {map_return_status_label(detail.get('estado'))}.",
            "bloques": [block("ESTADO_DEVOLUCION", _return_detail(detail))],
        })

    return {
        "preparar_reclamo": preparar_reclamo,
        "listar_reclamos": listar_reclamos,
        "consultar_reclamo": consultar_reclamo,
        "preparar_devolucion": preparar_devolucion,
        "listar_devoluciones": listar_devoluciones,
        "consultar_devolucion": consultar_devolucion,
    }


def build_postventa_confirmation_handlers(postventa: PostventaService) -> dict[str, Any]:
    async def confirmar_reclamo(args: ClaimConfirmArguments, context: ExecutionContext) -> ToolResult:
        caller = caller_from_context(context)
        conversation_id = conversation_id_from_context(context)
        if (
            caller is None or caller.customer_id is None
            or not context.access_token or conversation_id is None
        ):
            return _session_required("confirmar_reclamo", args)
        detalle = args.descripcion
        if args.solucionEsperada:
            detalle = f"{detalle}\n\nSolución esperada: {args.solucionEsperada}"
        payload = {
            "pedidoId": args.pedidoId,
            "tipo": args.tipo,
            "motivo": args.motivo,
            "detalle": detalle,
            "consumidor": args.consumidor.model_dump(),
            "forzar": args.forzar,
        }
        key = args.idempotencyKey or _idempotency_key("CONFIRMAR_RECLAMO", context, args)
        try:
            detail = await postventa.create_claim(
                customer_id=caller.customer_id,
                holder_token=context.access_token,
                payload=payload,
                idempotency_key=key,
                conversation_id=conversation_id,
            )
        except PostventaError as exc:
            if exc.code == "RECLAMO_DUPLICADO":
                return _duplicate_claim(exc)
            return _service_error(
                exc, tool="confirmar_reclamo", args=args,
                unavailable="No pudimos registrar tu reclamo en este momento.",
                not_found="No encontré ese pedido en tu cuenta.",
            )
        except (ModuleRequestError, ModuleUnavailableError) as exc:
            return _service_error(
                exc, tool="confirmar_reclamo", args=args,
                unavailable="No pudimos registrar tu reclamo en este momento. Intenta de nuevo.",
                not_found="No encontré ese pedido en tu cuenta.",
            )
        return ToolResult("OK", {
            "texto": "¡Registramos tu reclamo! " + CLAIM_CONSTANCIA_TEXT,
            "bloques": [block("CONSTANCIA_RECLAMO", _claim_constancia(detail))],
        })

    async def confirmar_devolucion(args: ReturnConfirmArguments, context: ExecutionContext) -> ToolResult:
        caller = caller_from_context(context)
        conversation_id = conversation_id_from_context(context)
        if (
            caller is None or caller.customer_id is None
            or not context.access_token or conversation_id is None
        ):
            return _session_required("confirmar_devolucion", args)
        payload = {
            "pedidoId": args.pedidoId,
            "tipo": args.tipo,
            "motivo": args.motivo,
            "descripcion": args.descripcion,
            "items": [item.model_dump() for item in args.items],
            "evidencias": [item.model_dump() for item in args.evidencias],
            "forzar": args.forzar,
        }
        key = args.idempotencyKey or _idempotency_key("CONFIRMAR_DEVOLUCION", context, args)
        try:
            detail = await postventa.create_return(
                customer_id=caller.customer_id,
                holder_token=context.access_token,
                payload=payload,
                idempotency_key=key,
                conversation_id=conversation_id,
            )
        except PostventaError as exc:
            if exc.code == "DEVOLUCION_DUPLICADA":
                return _duplicate_return(exc)
            return _service_error(
                exc, tool="confirmar_devolucion", args=args,
                unavailable="No pudimos registrar tu solicitud en este momento.",
                not_found="No encontré ese pedido en tu cuenta.",
            )
        except (ModuleRequestError, ModuleUnavailableError) as exc:
            return _service_error(
                exc, tool="confirmar_devolucion", args=args,
                unavailable="No pudimos registrar tu solicitud en este momento. Intenta de nuevo.",
                not_found="No encontré ese pedido en tu cuenta.",
            )
        return ToolResult("OK", {
            "texto": RETURN_CONSTANCIA_TEXT,
            "bloques": [block("CONSTANCIA_DEVOLUCION", _return_summary(detail))],
        })

    return {
        "confirmar_reclamo": confirmar_reclamo,
        "confirmar_devolucion": confirmar_devolucion,
    }


def build_postventa_confirmation_tools(postventa: PostventaService) -> tuple[ToolDefinition, ...]:
    handlers = build_postventa_confirmation_handlers(postventa)
    return (
        ToolDefinition(
            "confirmar_reclamo",
            "UI-only: registra el reclamo confirmado en el formulario.",
            ClaimConfirmArguments,
            handlers["confirmar_reclamo"],
            requiere_sesion=True,
            requiere_confirmacion=True,
            solo_ui=True,
        ),
        ToolDefinition(
            "confirmar_devolucion",
            "UI-only: registra la solicitud de devolución confirmada en el formulario.",
            ReturnConfirmArguments,
            handlers["confirmar_devolucion"],
            requiere_sesion=True,
            requiere_confirmacion=True,
            solo_ui=True,
        ),
    )
