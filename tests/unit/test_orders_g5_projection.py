"""G5 order detail shape -> channel response projection (SPEC-17)."""
import anyio

from src.modules.mod_orders.application.orders_service import OrdersService


class G5ShapeSales:
    async def get_customer_order(self, order_id, holder_token, *, correlation_id=None):
        return {
            "pedidoId": order_id,
            "canal": "CHATBOT",
            "estado": "ENTREGADO",
            "tipo": "VENTA",
            "contacto": {"clienteId": "11111111-1111-1111-1111-111111111111"},
            "pago": {"total": 299.9, "moneda": "PEN", "metodoPago": "TARJETA_CREDITO", "estado": "CONFIRMADO"},
            "envio": {"distrito": "Miraflores", "direccion": "Av. Larco 123"},
            "items": [
                {
                    "productoId": "P-1",
                    "sku": "SKU-1",
                    "descripcion": "Zapatilla",
                    "cantidad": 1,
                    "precioUnitario": 299.9,
                }
            ],
            "fechaCreacion": "2026-11-10T10:00:00Z",
            "ultimaActualizacion": "2026-11-11T10:00:00Z",
            "historialEstados": [
                {"estado": "CREADO", "fecha": "2026-11-10T10:00:00Z", "actor": "CANAL_CHATBOT", "motivo": "Registro"},
                {"estado": "ENTREGADO", "fecha": "2026-11-11T10:00:00Z", "actor": "SISTEMA_LOGISTICA", "motivo": "Entrega"},
            ],
        }


def test_g5_detail_projects_total_and_history():
    service = OrdersService(notifications=None, sales_client=G5ShapeSales())  # type: ignore[arg-type]

    async def run():
        return await service.get_order_detail(
            "11111111-1111-1111-1111-111111111111",
            "PED-2026-00001",
            holder_token="token",
        )

    result = anyio.run(run)
    assert result["total"] == 299.9
    assert result["moneda"] == "PEN"
    assert result["estadoEtiqueta"] == "Entregado"
    assert len(result["historial"]) == 2
    assert result["historial"][0]["etiqueta"] == "Pendiente de pago"
    assert result["historial"][1]["completado"] is True


class FakeDispatch:
    def __init__(self, payload=None):
        self.payload = payload

    async def get_order_tracking(self, order_id, *, correlation_id=None):
        if isinstance(self.payload, Exception):
            raise self.payload
        if self.payload is None:
            return None
        return self.payload


def test_tracking_uses_dispatch_after_verifying_ownership():
    service = OrdersService(
        notifications=None,
        sales_client=G5ShapeSales(),
        dispatch=FakeDispatch(
            {
                "pedidoId": "PED-2026-00001",
                "estado": "EN_CAMINO",
                "estadoEtiqueta": "En camino",
                "hitos": [
                    {
                        "titulo": "Recibido en centro de despacho",
                        "fecha": "2026-11-11T10:00:00-05:00",
                        "completado": True,
                    }
                ],
            }
        ),
    )

    async def run():
        return await service.get_order_tracking(
            "11111111-1111-1111-1111-111111111111",
            "PED-2026-00001",
            holder_token="token",
        )

    result = anyio.run(run)
    assert result["estado"] == "ENTREGADO"
    assert result["estadoEtiqueta"] == "Entregado"
    assert result["detalleDisponible"] is True
    assert result["distrito"] == "Miraflores"
    assert result["hitos"][-1]["titulo"] == "Entregado"


def test_tracking_without_dispatch_returns_sales_status():
    service = OrdersService(notifications=None, sales_client=G5ShapeSales())

    async def run():
        return await service.get_order_tracking(
            "11111111-1111-1111-1111-111111111111",
            "PED-2026-00001",
            holder_token="token",
        )

    result = anyio.run(run)
    assert result["estado"] == "ENTREGADO"
    assert result["estadoEtiqueta"] == "Entregado"
    assert result["detalleDisponible"] is False
    assert result["hitos"] == []
    assert result["mensaje"] == "El detalle del envío no está disponible ahora."


def test_tracking_404_from_dispatch_is_not_an_error():
    service = OrdersService(
        notifications=None,
        sales_client=G5ShapeSales(),
        dispatch=FakeDispatch(None),
    )

    async def run():
        return await service.get_order_tracking(
            "11111111-1111-1111-1111-111111111111",
            "PED-2026-00001",
            holder_token="token",
        )

    result = anyio.run(run)
    assert result["detalleDisponible"] is False
    assert result["estadoEtiqueta"] == "Entregado"


class PaidSales(G5ShapeSales):
    async def get_customer_order(self, order_id, holder_token, *, correlation_id=None):
        order = await super().get_customer_order(order_id, holder_token)
        order["estado"] = "PAGADO"
        return order


def test_tracking_does_not_use_dispatch_before_despachado():
    service = OrdersService(
        notifications=None,
        sales_client=PaidSales(),
        dispatch=FakeDispatch(
            {
                "pedidoId": "PED-2026-00001",
                "estado": "EN_CAMINO",
                "estadoEtiqueta": "En camino",
            }
        ),
    )

    async def run():
        return await service.get_order_tracking(
            "11111111-1111-1111-1111-111111111111",
            "PED-2026-00001",
            holder_token="token",
        )

    result = anyio.run(run)
    assert result["estado"] == "PAGADO"
    assert result["estadoEtiqueta"] == "Pago confirmado"
    assert result["detalleDisponible"] is False
    assert result["hitos"] == []
