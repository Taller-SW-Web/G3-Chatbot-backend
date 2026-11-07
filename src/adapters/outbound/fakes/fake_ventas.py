"""Fake in-process Ventas adapter for testing and SALES_MODE=fake."""
import base64
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from src.adapters.outbound.fakes.fake_security import ACCESS_TOKEN_PREFIX
from src.ports.outbound.module_errors import ModuleRequestError
from src.ports.outbound.order_repository_port import SalesOrderPort


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
        # Random segment: ids stay unique across restarts (order_ref.order_id is a primary key).
        order_id = f"PED-{datetime.now(timezone.utc).year}-{uuid.uuid4().hex[:6].upper()}"
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

    async def notify_payment(
        self,
        order_id: str,
        payload: dict[str, Any],
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        self.payment_notifications.append({"order_id": order_id, "payload": payload})
        if order_id in self.orders:
            self.orders[order_id]["estado"] = "PAGADO"
        return {
            "pedidoId": order_id,
            "nuevoEstado": "PAGADO",
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
        if order_id in self.orders:
            self.orders[order_id]["estado"] = "ANULADO"
        return {
            "pedidoId": order_id,
            "estadoPedido": "ANULADO",
            "autorizacionRequerida": False,
            "solicitudReembolsoGenerada": False,
        }

    async def list_customer_orders(
        self,
        holder_token: str,
        *,
        estado: str | None = None,
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
            "historial": [{"estado": order["estado"], "fecha": "2026-11-05T19:00:00Z", "completado": True}],
        }
