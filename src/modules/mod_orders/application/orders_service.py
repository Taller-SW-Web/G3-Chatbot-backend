"""Customer orders: listing, detail, tracking and confirmation resend (SPEC-17, SPEC-18).

`OrdersService` is the module's facade behind `OrdersServicePort`: it delegates
order reads to Ventas with the holder token and the e-mail resend to
`NotificationService`. Tracking degrades to the Ventas state whenever Despacho
has no detail yet (404, missing scope or timeout).
"""
from typing import Any

from src.modules.mod_orders.application.notification_service import (
    NotificationError,
    NotificationService,
)
from src.modules.mod_orders.domain.cancellation import (
    cancellation_message,
    cancellation_reason,
    refund_message,
    safe_reason,
)
from src.modules.mod_orders.domain.status_mapper import map_order_status_label
from src.shared.application.ports.order_repository_port import SalesOrderPort
from src.shared.domain.module_errors import ModuleRequestError, ModuleUnavailableError


def _order_history(order: dict[str, Any]) -> list[Any]:
    """Status history of a Ventas order (`historial` mock alias or G5 `historialEstados`)."""
    return order.get("historial") or order.get("historialEstados") or []


def _with_status_label(payload: dict, key: str = "estadoEtiqueta") -> dict:
    """Adds the customer-facing label Ventas does not send (contratos-integracion §2.5.2)."""
    if payload.get(key):
        return payload
    return {**payload, key: map_order_status_label(str(payload.get("estado") or ""))}


def _cancelled_label(history: list[dict[str, Any]]) -> str:
    """SPEC-17 §4.1: the cancellation label carries the date of the ANULADO milestone."""
    for item in reversed(history):
        if str(item.get("estado") or "").upper() == "ANULADO":
            fecha = str(item.get("fecha") or "").strip()
            return f"Anulado el {fecha}" if fecha else "Anulado"
    return "Anulado"


def _order_customer_id(order: dict[str, Any]) -> str | None:
    """Owner of a Ventas order (`contacto.clienteId`, or a root `clienteId`)."""
    contact = order.get("contacto")
    candidate = contact.get("clienteId") if isinstance(contact, dict) else None
    if candidate in (None, ""):
        candidate = order.get("clienteId")
    return str(candidate) if candidate not in (None, "") else None


def _ensure_owner(
    order: dict[str, Any], customer_id, ref: Any | None = None
) -> None:
    """SPEC-17 RNF: a foreign order looks exactly like an unknown one (404).

    Ventas already filters with the holder token; the BFF repeats the check with
    every ownership signal it has (`contacto.clienteId` and the local
    `order_ref.customer_id`) so a permissive/upstream bug never leaks the order.
    """
    owners = {
        owner
        for owner in (
            _order_customer_id(order),
            str(getattr(ref, "customer_id", None) or "") or None,
        )
        if owner
    }
    if owners and any(owner != str(customer_id) for owner in owners):
        raise NotificationError("RECURSO_NO_ENCONTRADO", "No encontré ese pedido en tu cuenta.")


class OrdersService:
    """`OrdersServicePort` for the orders router: confirmation resend (SPEC-16) and
    customer orders listing / detail (SPEC-17) delegated to Ventas."""

    def __init__(
        self,
        notifications: NotificationService,
        sales_client: SalesOrderPort | None = None,
        dispatch=None,
        order_refs=None,
    ):
        self._notifications = notifications
        self._sales = sales_client
        self._dispatch = dispatch
        self._order_refs = order_refs

    async def resend_confirmation(self, customer_id, order_id, *, holder_token, idempotency_key):
        return await self._notifications.resend_confirmation(
            customer_id, order_id, idempotency_key=idempotency_key
        )

    async def list_orders(
        self,
        customer_id,
        *,
        holder_token: str,
        estado: str | None = None,
        desde: str | None = None,
        hasta: str | None = None,
        page: int = 1,
        page_size: int = 10,
    ):
        if self._sales:
            raw = await self._sales.list_customer_orders(
                holder_token,
                estado=estado,
                desde=desde,
                hasta=hasta,
                page=page,
                page_size=page_size,
            )
            return {**raw, "items": [_with_status_label(item) for item in raw.get("items", [])]}
        raise NotificationError("SERVICIO_NO_DISPONIBLE", "La consulta de pedidos aún no está disponible.")

    async def get_order_detail(
        self,
        customer_id,
        order_id: str,
        *,
        holder_token: str,
    ):
        if self._sales:
            raw = await self._sales.get_customer_order(order_id, holder_token)
            ref = (
                await self._order_refs.obtener_por_order_id(order_id)
                if self._order_refs is not None
                else None
            )
            # SPEC-17 RNF: the BFF compares ownership before projecting the detail.
            _ensure_owner(raw, customer_id, ref)
            # G5 `PedidoDetalle` keeps money inside `pago` and history in
            # `historialEstados`; the channel contract exposes them at the root.
            payment_raw = raw.get("pago")
            payment: dict[str, Any] = payment_raw if isinstance(payment_raw, dict) else {}
            total = raw.get("total", payment.get("total", 0))
            moneda = raw.get("moneda", payment.get("moneda", "PEN"))
            history = _order_history(raw)
            reason = cancellation_reason(history)
            # `historialEstados` is replaced by the sanitized `historial` below (no raw G5 text).
            public_raw = {k: v for k, v in raw.items() if k != "historialEstados"}
            detail = {
                **_with_status_label({**public_raw, "total": total, "moneda": moneda}),
                "motivoAnulacion": reason if str(raw.get("estado") or "").upper() == "ANULADO" else None,
                "reembolso": refund_message(str(raw.get("estado") or ""), reason, payment, history),
                "historial": [
                    {
                        **_with_status_label(m, key="etiqueta"),
                        # Never pass G5 free text through: only published reason codes.
                        "motivo": safe_reason(m.get("motivo")),
                        "completado": m.get("completado", True),
                    }
                    for m in history
                ],
            }
            if str(raw.get("estado") or "").upper() == "ANULADO":
                label = _cancelled_label(history)
                detail["estadoEtiqueta"] = label
                for item in detail["historial"]:
                    if str(item.get("estado") or "").upper() == "ANULADO":
                        item["etiqueta"] = label
            if self._order_refs is not None and ref is not None:
                # SPEC-24: the frontend hides the CSAT block without asking Ventas again.
                detail["csatEnviado"] = bool(ref.csat_sent)
            return detail
        raise NotificationError("SERVICIO_NO_DISPONIBLE", "La consulta de pedidos aún no está disponible.")

    async def get_order_tracking(
        self,
        customer_id,
        order_id: str,
        *,
        holder_token: str,
    ):
        """Order tracking (SPEC-18).

        First proves ownership through Ventas with the customer token, then asks
        Despacho with the service token. Despacho is optional: a 404 while the
        order is still being prepared, a missing scope or a timeout degrade to
        the order state from Ventas with ``detalleDisponible=false``.
        """
        if self._sales is None:
            raise NotificationError(
                "SERVICIO_NO_DISPONIBLE",
                "La consulta de pedidos aún no está disponible.",
            )
        order = await self._sales.get_customer_order(order_id, holder_token)
        ref = (
            await self._order_refs.obtener_por_order_id(order_id)
            if self._order_refs is not None
            else None
        )
        # Same anti-enumeration check as the detail: a foreign order is a 404.
        _ensure_owner(order, customer_id, ref)
        estado = str(order.get("estado") or "").strip().upper()
        envio_raw = order.get("envio")
        envio: dict[str, Any] = envio_raw if isinstance(envio_raw, dict) else {}
        history = _order_history(order)
        fallback: dict[str, Any] = {
            "pedidoId": order_id,
            "estado": estado,
            "estadoEtiqueta": _cancelled_label(history) if estado == "ANULADO" else map_order_status_label(estado),
            "fechaProgramada": envio.get("fechaEstimadaEntrega"),
            "distrito": envio.get("distrito"),
            "hitos": [],
            "detalleDisponible": False,
            "mensaje": (
                "Tu pedido está pendiente de pago."
                if estado == "CREADO"
                else "Tu pedido aún se está preparando; te mostraremos el seguimiento "
                "cuando salga del centro de despacho."
                if estado in {"PAGADO", "EN_PREPARACION"}
                else "El detalle del envío no está disponible ahora."
            ),
        }
        if estado == "ANULADO":
            payment_raw = order.get("pago")
            payment: dict[str, Any] = payment_raw if isinstance(payment_raw, dict) else {}
            reason = cancellation_reason(history)
            refund = refund_message(estado, reason, payment, history)
            fallback.update(
                mensaje=cancellation_message(reason, refund),
                motivoAnulacion=reason,
                reembolso=refund,
            )
        # Ventas is the source of truth. Despacho only adds detail once the
        # order is actually dispatched (or delivered); a paid order still in
        # preparation must not show a synthetic "on the way" milestone.
        if estado not in {"DESPACHADO", "ENTREGADO"}:
            return fallback
        if self._dispatch is None:
            return fallback
        try:
            raw = await self._dispatch.get_order_tracking(order_id)
        except (ModuleRequestError, ModuleUnavailableError):
            # A 404 is normal before dispatch, and a missing scope or timeout
            # must not hide the order status the customer can still see.
            return fallback
        if not isinstance(raw, dict) or not raw:
            return fallback
        tracking = dict(raw)
        if estado == "ENTREGADO":
            tracking["estado"] = "ENTREGADO"
            tracking["estadoEtiqueta"] = map_order_status_label("ENTREGADO")
            hitos = tracking.get("hitos")
            if isinstance(hitos, list) and not any(
                str(hito.get("etiqueta") or hito.get("titulo") or "").strip().casefold()
                == "entregado"
                for hito in hitos
                if isinstance(hito, dict)
            ):
                delivered_at = next(
                    (
                        str(item.get("fecha"))
                        for item in history
                        if isinstance(item, dict) and item.get("estado") == "ENTREGADO"
                    ),
                    "",
                )
                hitos.append(
                    {
                        "titulo": "Entregado",
                        "fecha": delivered_at,
                        "completado": True,
                    }
                )
        tracking.setdefault("pedidoId", order_id)
        tracking.setdefault("distrito", envio.get("distrito"))
        tracking.setdefault("fechaProgramada", envio.get("fechaEstimadaEntrega"))
        tracking.setdefault("detalleDisponible", True)
        return tracking
