"""Shipping quote and saved addresses (`SPEC-12`).

Orchestrates the customer's cart, Seguridad and Despacho behind their ports:
the cart supplies the lines (the client never sends them), Seguridad the saved
addresses used to prefill the checkout, and Despacho the real coverage, cost
and lead time. The HTTP adapter only validates shapes and maps errors; nothing
here imports FastAPI, httpx or SQLAlchemy.
"""
import uuid
from typing import Any

from src.application.dto.commerce_contracts_dto import ShippingQuoteRequest
from src.ports.outbound.module_errors import ModuleRequestError

# Despacho's own code for a quote without lines; validating it here avoids a
# pointless round trip and the shipping router maps it to `400 VALIDACION`.
EMPTY_CART_CODE = "DESP_ERROR_LINEAS_VACIAS"


class ManageShippingUseCase:
    def __init__(self, carts, items, dispatch, security) -> None:
        self._carts = carts
        self._items = items
        self._dispatch = dispatch
        self._security = security

    async def list_addresses(
        self, customer_id: Any, *, holder_token: str
    ) -> list[dict[str, Any]]:
        """Saved addresses of the customer, proxied from Seguridad (`SPEC-12`)."""
        return await self._security.list_addresses(str(customer_id), holder_token)

    async def quote_shipping(
        self, customer_id: Any, payload: ShippingQuoteRequest
    ) -> dict[str, Any]:
        """Quote the active cart to the given address (`SPEC-12` Req. 2 and 4).

        Returns Despacho's raw body: `coberturaDisponible`, `costoEnvio`,
        `moneda`, `plazoEstimadoDiasHabiles`, `fechaEstimadaEntrega` and
        `nombreZona`. The router turns it into the channel response.
        """
        destino = {
            "destinatario": payload.destinatario,
            "direccion": payload.direccion,
            "distrito": payload.distrito,
            "departamento": payload.departamento,
            "provincia": payload.provincia,
            "referencia": payload.referencia,
        }
        return await self._dispatch.quote_shipping(
            destino=destino,
            lineas=await self._cart_lines(customer_id),
            correlation_id=str(uuid.uuid4()),
        )

    async def _cart_lines(self, customer_id: Any) -> list[dict[str, Any]]:
        cart = await self._carts.obtener_abierto_por_cliente(customer_id)
        rows = await self._items.listar_por_cart(cart.id) if cart is not None else []
        lineas = [
            {"sku": row.sku, "cantidad": row.quantity}
            for row in rows
            if row.quantity > 0
        ]
        if not lineas:
            raise ModuleRequestError(
                "dispatch",
                400,
                EMPTY_CART_CODE,
                "El carrito no tiene líneas para cotizar.",
            )
        return lineas
