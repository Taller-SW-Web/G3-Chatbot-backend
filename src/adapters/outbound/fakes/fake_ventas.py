"""Fake in-process Ventas adapter for testing and SALES_MODE=fake."""
from typing import Any

from src.ports.outbound.order_repository_port import SalesOrderPort


class FakeSalesPort(SalesOrderPort):
    def __init__(self):
        self.orders: dict[str, dict[str, Any]] = {}
        self.created_calls: list[dict[str, Any]] = []
        self.payment_notifications: list[dict[str, Any]] = []
        self.cancellations: list[dict[str, Any]] = []

    async def create_order(
        self,
        snapshot: dict[str, Any],
        *,
        idempotency_key: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        order_id = f"PED-{len(self.orders) + 1000}"
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
        self.created_calls.append({"snapshot": snapshot, "idempotency_key": idempotency_key})
        return {
            "pedidoId": order_id,
            "estado": "CREADO",
            "total": total,
            "moneda": "PEN",
            "canal": snapshot.get("canal", "CHATBOT"),
            "fechaCreacion": "2026-11-05T19:00:00Z",
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
            if not estado or o["estado"] == estado
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
        if not order:
            from src.ports.outbound.module_errors import ModuleRequestError
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
