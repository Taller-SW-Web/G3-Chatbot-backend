"""Outbound port: Despacho y Entrega module (`SPEC-12`, `SPEC-18`, `contratos-integracion.md` §3.4).

Authenticated with the `modulo-chatbot` service token (`client_credentials`,
audience `api-despacho`, scopes `cotizaciones:calcular` and `seguimientos:leer`,
agreements A4, A11, A12).
"""
from typing import Any, Protocol


class DispatchPort(Protocol):
    async def quote_shipping(
        self,
        *,
        destino: dict[str, Any],
        lineas: list[dict[str, Any]],
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Calls `POST /api/v1/cotizaciones` (timeout 4 s, `SPEC-12` Req. 4).

        Returns `{coberturaDisponible, costoEnvio, moneda, plazoEstimadoDiasHabiles,
        fechaEstimadaEntrega, nombreZona, tipoCotizacion}`.
        """
        ...

    async def get_order_tracking(
        self,
        order_id: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Calls `GET /api/v1/seguimientos/pedidos/{idPedido}` (`SPEC-18` Req. 1).

        Returns `None` when Despacho answers 404 (order still in preparation).
        """
        ...
