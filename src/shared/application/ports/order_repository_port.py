"""Outbound ports: Ventas orders API (`SPEC-15`, `SPEC-17`) and local `order_ref` persistence.

Authentication rule (`contratos-integracion.md` §1, §3.3, agreement A15):
- System operations (`create_order`, `notify_payment`, `cancel_order`) use the
  channel service token (`modulo-chatbot` with scopes `ventas:pedidos:crear`,
  `ventas:pagos:webhook`, `ventas:pedidos:anular`).
- Customer read operations (`list_customer_orders`, `get_customer_order`) forward
  the customer's Bearer token (`rol: CLIENTE`, filtered by `sub` in Ventas).
"""
import uuid
from typing import Any, Protocol


class SalesOrderPort(Protocol):
    async def create_order(
        self,
        snapshot: dict[str, Any],
        *,
        idempotency_key: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """`POST /api/v1/pedidos` (`SPEC-15` Req. 1). Returns `{pedidoId, estado, total, ...}`."""
        ...

    async def notify_payment(
        self,
        order_id: str,
        payload: dict[str, Any],
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """`POST /api/v1/pedidos/{id}/pagos/notificacion` (`SPEC-15` Req. 2)."""
        ...

    async def cancel_order(
        self,
        order_id: str,
        *,
        motivo: str = "PAGO_NO_COMPLETADO",
        comentario: str = "Tiempo límite de espera agotado; cancelado por el canal",
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """`POST /api/v1/pedidos/{id}/anulaciones` (`SPEC-15` Req. 3)."""
        ...

    async def list_customer_orders(
        self,
        holder_token: str,
        *,
        estado: str | None = None,
        page: int = 1,
        page_size: int = 10,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """`GET /api/v1/pedidos/me` with the customer's Bearer token (A15, `SPEC-17` Req. 1)."""
        ...

    async def get_customer_order(
        self,
        order_id: str,
        holder_token: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """`GET /api/v1/pedidos/{id}` with the customer's Bearer token (A15, `SPEC-17` Req. 1)."""
        ...


class OrderRefRepositoryPort(Protocol):
    async def guardar(self, row: Any) -> Any:
        ...

    async def obtener_por_order_id(self, order_id: str) -> Any | None:
        ...

    async def obtener_por_checkout_id(self, checkout_id: uuid.UUID) -> Any | None:
        ...

    async def actualizar_estado(self, order_id: str, local_status: str) -> Any | None:
        ...
