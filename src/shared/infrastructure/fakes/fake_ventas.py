"""Fake in-process Ventas adapter for testing and SALES_MODE=fake."""
import base64
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from src.shared.application.ports.order_repository_port import SalesOrderPort
from src.shared.domain.module_errors import ModuleRequestError
from src.shared.infrastructure.fakes.fake_security import ACCESS_TOKEN_PREFIX


def _holder_id(holder_token: str) -> str:
    """Customer id behind a holder token: fake `fake-access-<id>` or the `sub` of a JWT (unverified)."""
    if holder_token.startswith(ACCESS_TOKEN_PREFIX):
        return holder_token.removeprefix(ACCESS_TOKEN_PREFIX)
    try:
        payload = holder_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(claims.get("sub") or holder_token)
    except (IndexError, ValueError):
        return holder_token


STOCK_CANCELLATION_REASON = "STOCK_NO_DISPONIBLE"
STOCK_CANCELLATION_ACTOR = "SISTEMA_PRODUCTOS"


class FakeSalesPort(SalesOrderPort):
    def __init__(self):
        self.orders: dict[str, dict[str, Any]] = {}
        self.created_calls: list[dict[str, Any]] = []
        self.payment_notifications: list[dict[str, Any]] = []
        self.cancellations: list[dict[str, Any]] = []
        self._order_by_key: dict[str, str] = {}
        self._owners: dict[str, str] = {}

    async def create_order(
        self,
        snapshot: dict[str, Any],
        *,
        idempotency_key: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        existing_id = self._order_by_key.get(idempotency_key)
        if existing_id is not None:
            return self._creation_response(self.orders[existing_id])
        # Random segment: ids stay unique across restarts and load runs (order_ref.order_id is a PK).
        order_id = f"PED-{datetime.now(timezone.utc).year}-{uuid.uuid4().hex[:10].upper()}"
        total = snapshot.get("pago", {}).get("total", 0.0)
        order_data = {
            "pedidoId": order_id,
            "estado": "CREADO",
            "canal": snapshot.get("canal", "CHATBOT"),
            "total": total,
            "moneda": "PEN",
            "fechaCreacion": "2026-11-05T19:00:00Z",
            "items": snapshot.get("items", []),
            "envio": snapshot.get("envio", {}),
            "contacto": snapshot.get("contacto", {}),
            # G5 `PedidoDetalle` shape: payment block and status history.
            "pago": {
                "total": total,
                "moneda": "PEN",
                "metodoPago": snapshot.get("pago", {}).get("metodoPago"),
                "estado": "PENDIENTE",
            },
            "historialEstados": [
                {
                    "estado": "CREADO",
                    "fecha": "2026-11-05T19:00:00Z",
                    "actor": "CANAL_CHATBOT",
                    "motivo": "Registro",
                }
            ],
        }
        self.orders[order_id] = order_data
        self._order_by_key[idempotency_key] = order_id
        self._owners[order_id] = str(order_data["contacto"].get("clienteId", ""))
        self.created_calls.append({"snapshot": snapshot, "idempotency_key": idempotency_key})
        return self._creation_response(order_data)

    @staticmethod
    def _creation_response(order: dict[str, Any]) -> dict[str, Any]:
        return {
            "pedidoId": order["pedidoId"],
            "estado": "CREADO",
            "total": order["total"],
            "moneda": "PEN",
            "canal": order["canal"],
            "fechaCreacion": order["fechaCreacion"],
        }

    def cancel_for_stock(self, order_id: str, *, paid: bool = True) -> None:
        """Simulates G5 cancelling the order for lack of stock (actor `SISTEMA_PRODUCTOS`).

        `paid=True` models a cancellation after the payment was confirmed (refund
        pending); `paid=False` leaves the payment pending, so a later approved
        payment is a late payment on an `ANULADO` order.
        """
        order = self.orders[order_id]
        order["estado"] = "ANULADO"
        order["pago"]["estado"] = "CONFIRMADO" if paid else "PENDIENTE"
        if paid:
            self._record_state(order, "PAGADO", "PASARELA_PAGOS", "Pago confirmado")
        self._record_state(order, "ANULADO", STOCK_CANCELLATION_ACTOR, STOCK_CANCELLATION_REASON)

    @staticmethod
    def _record_state(order: dict[str, Any], state: str, actor: str, reason: str | None) -> None:
        order["historialEstados"].append(
            {"estado": state, "fecha": "2026-11-05T19:05:00Z", "actor": actor, "motivo": reason}
        )

    async def notify_payment(
        self,
        order_id: str,
        payload: dict[str, Any],
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        self.payment_notifications.append({"order_id": order_id, "payload": payload})
        new_state = "PAGADO"
        order = self.orders.get(order_id)
        if order is not None:
            if order["estado"] == "ANULADO":
                # G5: an approved payment on a cancelled order is CONFIRMADO, the
                # order stays ANULADO and a refund is requested.
                new_state = "ANULADO"
                order["pago"]["estado"] = "CONFIRMADO"
            else:
                order["estado"] = "PAGADO"
                order["pago"]["estado"] = "CONFIRMADO"
                self._record_state(order, "PAGADO", "PASARELA_PAGOS", None)
        return {
            "pedidoId": order_id,
            "nuevoEstado": new_state,
            "transaccionId": payload.get("transaccionId", "SIM-1"),
            "fechaTransicion": "2026-11-05T19:05:00Z",
        }

    async def cancel_order(
        self,
        order_id: str,
        *,
        motivo: str = "PAGO_NO_COMPLETADO",
        comentario: str = "Tiempo límite de espera agotado; cancelado por el canal",
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        self.cancellations.append({"order_id": order_id, "motivo": motivo, "comentario": comentario})
        order = self.orders.get(order_id)
        if order is not None:
            if order["estado"] == "ANULADO":
                # Same error the HTTP client raises for the G5 409.
                raise ModuleRequestError("sales", 409, "PEDIDO_NO_ANULABLE")
            order["estado"] = "ANULADO"
            self._record_state(order, "ANULADO", "CANAL_CHATBOT", motivo)
        return {
            "pedidoId": order_id,
            "estadoPedido": "ANULADO",
            "autorizacionRequerida": False,
            "solicitudReembolsoGenerada": False,
        }

    async def get_order_as_service(
        self,
        order_id: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        order = self.orders.get(order_id)
        if not order:
            raise ModuleRequestError("sales", 404, "PEDIDO_NO_ENCONTRADO")
        return {
            "pedidoId": order["pedidoId"],
            "canal": order["canal"],
            "estado": order["estado"],
            "total": order["total"],
            "moneda": order["moneda"],
            "fechaCreacion": order["fechaCreacion"],
            "items": order["items"],
            "envio": order["envio"],
            "pago": dict(order["pago"]),
            "historialEstados": self._history(order),
        }

    async def list_customer_orders(
        self,
        holder_token: str,
        *,
        estado: str | None = None,
        desde: str | None = None,
        hasta: str | None = None,
        page: int = 1,
        page_size: int = 10,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        holder = _holder_id(holder_token)
        items = [
            {
                "pedidoId": o["pedidoId"],
                "canal": o["canal"],
                "estado": o["estado"],
                "total": o["total"],
                "moneda": o["moneda"],
                "fechaCreacion": o["fechaCreacion"],
            }
            for o in self.orders.values()
            if self._owners.get(o["pedidoId"]) == holder and (not estado or o["estado"] == estado)
        ]
        return {
            "items": items,
            "total": len(items),
            "page": page,
            "pageSize": page_size,
        }

    async def get_customer_order(
        self,
        order_id: str,
        holder_token: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        order = self.orders.get(order_id)
        # A foreign order looks the same as an unknown one (SPEC-17 Req. 1).
        if not order or self._owners.get(order_id) != _holder_id(holder_token):
            raise ModuleRequestError("sales", 404, "RECURSO_NO_ENCONTRADO")
        return {
            "pedidoId": order["pedidoId"],
            "canal": order["canal"],
            "estado": order["estado"],
            "total": order["total"],
            "moneda": order["moneda"],
            "fechaCreacion": order["fechaCreacion"],
            "items": order["items"],
            "envio": order["envio"],
            "pago": dict(order["pago"]),
            "historialEstados": self._history(order),
        }

    @staticmethod
    def _history(order: dict[str, Any]) -> list[dict[str, Any]]:
        """Stored history plus the current state when a test set `estado` directly."""
        history = [dict(entry) for entry in order["historialEstados"]]
        if history[-1]["estado"] != order["estado"]:
            history.append(
                {"estado": order["estado"], "fecha": "2026-11-05T19:00:00Z", "actor": "FAKE", "motivo": None}
            )
        return history
