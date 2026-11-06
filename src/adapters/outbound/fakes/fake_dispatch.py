"""Despacho fake para entornos desplegados sin G2 (ADR-0022, Hito 4).

Cubre los distritos de Lima Metropolitana que ofrece el checkout con un costo y
un plazo fijos (los mismos S/ 12.50 y 1 día que mostraba la pantalla antes de la
integración), responde sin cobertura para el resto y devuelve hitos simples de
seguimiento. Así `DISPATCH_MODE=fake` permite demostrar SPEC-12 y SPEC-18 sin
las credenciales reales de Despacho.

Es determinista a propósito: la demo no depende de la red ni de G2.
"""
from datetime import date, datetime, timedelta
from typing import Any

from src.ports.outbound.module_errors import ModuleRequestError

# Distritos con cobertura del mock: los mismos que ofrece el checkout.
DISTRITOS_CON_COBERTURA = frozenset({
    "Lima Cercado",
    "Miraflores",
    "San Isidro",
    "Santiago de Surco",
    "La Molina",
    "San Miguel",
    "Los Olivos",
    "San Juan de Lurigancho",
    "Villa El Salvador",
    "Callao",
    "Jesús María",
    "Lince",
    "Magdalena del Mar",
})
COSTO_ENVIO = 12.50
PLAZO_DIAS = 1


class FakeDispatchPort:
    """`DispatchPort` en memoria (SPEC-12, SPEC-18)."""

    module = "dispatch"

    async def quote_shipping(
        self,
        *,
        destino: dict[str, Any],
        lineas: list[dict[str, Any]],
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        destino = destino or {}
        if not str(destino.get("destinatario") or "").strip() or not str(
            destino.get("direccion") or ""
        ).strip():
            raise ModuleRequestError(
                self.module, 400, "DESP_ERROR_DESTINO_REQUERIDO"
            )
        if not lineas:
            raise ModuleRequestError(self.module, 400, "DESP_ERROR_LINEAS_VACIAS")
        distrito = str(destino.get("distrito") or "").strip()
        if distrito not in DISTRITOS_CON_COBERTURA:
            return {"coberturaDisponible": False, "moneda": "PEN", "distrito": distrito}
        return {
            "coberturaDisponible": True,
            "costoEnvio": COSTO_ENVIO,
            "moneda": "PEN",
            "plazoEstimadoDiasHabiles": PLAZO_DIAS,
            "fechaEstimadaEntrega": (date.today() + timedelta(days=PLAZO_DIAS)).isoformat(),
            "nombreZona": "Lima Metropolitana",
            "tipoCotizacion": "DELIVERY",
            "distrito": distrito,
        }

    async def get_order_tracking(
        self, order_id: str, *, correlation_id: str | None = None
    ) -> dict[str, Any] | None:
        if not order_id.startswith("PED-"):
            return None
        ahora = datetime.now().isoformat(timespec="minutes")
        return {
            "pedidoId": order_id,
            "estado": "EN_CAMINO",
            "estadoEtiqueta": "En camino",
            "fechaProgramada": (date.today() + timedelta(days=PLAZO_DIAS)).isoformat(),
            "hitos": [
                {
                    "titulo": "Recibido en centro de despacho",
                    "fecha": ahora,
                    "completado": True,
                },
                {"titulo": "En camino", "fecha": ahora, "completado": True},
            ],
        }
