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
from src.modules.mod_orders.domain.status_mapper import map_order_status_label
from src.shared.application.ports.order_repository_port import SalesOrderPort
from src.shared.domain.module_errors import ModuleRequestError, ModuleUnavailableError


def _with_status_label(payload: dict, key: str = "estadoEtiqueta") -> dict:
    """Adds the customer-facing label Ventas does not send (contratos-integracion §2.5.2)."""
    if payload.get(key):
        return payload
    return {**payload, key: map_order_status_label(str(payload.get("estado") or ""))}


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
        page: int = 1,
        page_size: int = 10,
    ):
        if self._sales:
            raw = await self._sales.list_customer_orders(
                holder_token,
                estado=estado,
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
            # G5 `PedidoDetalle` keeps money inside `pago` and history in
            # `historialEstados`; the channel contract exposes them at the root.
            payment_raw = raw.get("pago")
            payment: dict[str, Any] = payment_raw if isinstance(payment_raw, dict) else {}
            total = raw.get("total", payment.get("total", 0))
            moneda = raw.get("moneda", payment.get("moneda", "PEN"))
            history = raw.get("historial") or raw.get("historialEstados") or []
            detail = {
                **_with_status_label({**raw, "total": total, "moneda": moneda}),
                "historial": [
                    {**_with_status_label(m, key="etiqueta"), "completado": m.get("completado", True)}
                    for m in history
                ],
            }
            if self._order_refs is not None:
                ref = await self._order_refs.obtener_por_order_id(order_id)
                if ref is not None:
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
        estado = str(order.get("estado") or "").strip().upper()
        envio_raw = order.get("envio")
        envio: dict[str, Any] = envio_raw if isinstance(envio_raw, dict) else {}
        fallback: dict[str, Any] = {
            "pedidoId": order_id,
            "estado": estado,
            "estadoEtiqueta": map_order_status_label(estado),
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
                else "Tu pedido fue anulado."
                if estado == "ANULADO"
                else "El detalle del envío no está disponible ahora."
            ),
        }
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
                str(hito.get("titulo", "")).strip().casefold() == "entregado"
                for hito in hitos
                if isinstance(hito, dict)
            ):
                delivered_at = next(
                    (
                        str(item.get("fecha"))
                        for item in order.get("historial") or []
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
        elif estado == "ANULADO":
            tracking["estado"] = "ANULADO"
            tracking["estadoEtiqueta"] = map_order_status_label("ANULADO")
        tracking.setdefault("pedidoId", order_id)
        tracking.setdefault("distrito", envio.get("distrito"))
        tracking.setdefault("fechaProgramada", envio.get("fechaEstimadaEntrega"))
        tracking.setdefault("detalleDisponible", True)
        return tracking
