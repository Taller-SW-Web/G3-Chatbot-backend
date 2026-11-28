"""G5 order detail shape -> channel response projection (SPEC-17)."""
import anyio
import pytest

from src.modules.mod_orders.application.notification_service import NotificationError
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
    assert "csatEnviado" not in result


def test_g5_detail_exposes_the_local_csat_flag():
    from types import SimpleNamespace

    class Refs:
        async def obtener_por_order_id(self, order_id):
            return SimpleNamespace(csat_sent=True)

    service = OrdersService(
        notifications=None, sales_client=G5ShapeSales(), order_refs=Refs(),  # type: ignore[arg-type]
    )

    async def run():
        return await service.get_order_detail(
            "11111111-1111-1111-1111-111111111111",
            "PED-2026-00001",
            holder_token="token",
        )

    assert anyio.run(run)["csatEnviado"] is True


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
                        "etiqueta": "Recibido en centro de despacho",
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


class CancelledSales(G5ShapeSales):
    """G5 order cancelled by the system; `reason`/`payment_status` vary per test."""

    def __init__(self, reason="STOCK_NO_DISPONIBLE", payment_status="CONFIRMADO", paid_in_history=False):
        self.reason = reason
        self.payment_status = payment_status
        self.paid_in_history = paid_in_history

    async def get_customer_order(self, order_id, holder_token, *, correlation_id=None):
        order = await super().get_customer_order(order_id, holder_token)
        history = [order["historialEstados"][0]]
        if self.paid_in_history:
            history.append({"estado": "PAGADO", "fecha": "2026-11-10T10:01:00Z", "actor": "PASARELA_PAGOS", "motivo": None})
        history.append(
            {"estado": "ANULADO", "fecha": "2026-11-10T10:05:00Z", "actor": "SISTEMA_PRODUCTOS", "motivo": self.reason}
        )
        order.update(estado="ANULADO", historialEstados=history)
        order["pago"]["estado"] = self.payment_status
        return order


G5_CUSTOMER_ID = "11111111-1111-1111-1111-111111111111"


def _detail(sales):
    service = OrdersService(notifications=None, sales_client=sales)  # type: ignore[arg-type]
    return anyio.run(lambda: service.get_order_detail(G5_CUSTOMER_ID, "PED-1", holder_token="t"))


def _tracking(sales):
    service = OrdersService(notifications=None, sales_client=sales)  # type: ignore[arg-type]
    return anyio.run(lambda: service.get_order_tracking(G5_CUSTOMER_ID, "PED-1", holder_token="t"))


REFUND = "Se procesará el reembolso al medio de pago original."


def test_detail_of_a_stock_cancelled_order_exposes_reason_and_refund():
    result = _detail(CancelledSales())

    assert result["motivoAnulacion"] == "STOCK_NO_DISPONIBLE"
    assert result["reembolso"] == REFUND
    assert result["historial"][-1]["motivo"] == "STOCK_NO_DISPONIBLE"


def test_detail_of_an_order_that_is_not_cancelled_has_no_reason_or_refund():
    result = _detail(G5ShapeSales())

    assert result["motivoAnulacion"] is None
    assert result["reembolso"] is None


def test_detail_refund_applies_when_the_payment_was_confirmed_or_the_order_was_paid():
    confirmed = _detail(CancelledSales(reason="SOLICITUD_CLIENTE"))
    paid_before = _detail(CancelledSales(reason="SOLICITUD_CLIENTE", payment_status="PENDIENTE", paid_in_history=True))

    assert confirmed["motivoAnulacion"] == "SOLICITUD_CLIENTE"
    assert confirmed["reembolso"] == REFUND
    assert paid_before["reembolso"] == REFUND


def test_detail_without_payment_evidence_has_no_refund_even_when_stock_cancelled():
    unpaid = _detail(CancelledSales(reason="PAGO_NO_COMPLETADO", payment_status="PENDIENTE"))
    stock = _detail(CancelledSales(payment_status="PENDIENTE"))

    assert unpaid["motivoAnulacion"] == "PAGO_NO_COMPLETADO"
    assert unpaid["reembolso"] is None
    assert stock["motivoAnulacion"] == "STOCK_NO_DISPONIBLE"
    assert stock["reembolso"] is None


def test_detail_hides_a_free_text_reason_from_the_reason_field_and_the_history():
    result = _detail(CancelledSales(reason="Error interno: fallo en bodega 7"))

    assert result["estado"] == "ANULADO"
    assert result["motivoAnulacion"] is None
    assert result["historial"][-1]["motivo"] is None
    assert result["reembolso"] == REFUND
    assert "Error interno" not in str(result)


def test_tracking_of_a_stock_cancelled_order_explains_reason_and_refund():
    result = _tracking(CancelledSales())

    assert result["estado"] == "ANULADO"
    assert result["detalleDisponible"] is False
    assert result["motivoAnulacion"] == "STOCK_NO_DISPONIBLE"
    assert result["reembolso"] == REFUND
    assert result["mensaje"] == f"Tu pedido fue anulado por falta de stock. {REFUND}"


def test_tracking_of_an_unpaid_stock_cancelled_order_has_no_refund_sentence():
    result = _tracking(CancelledSales(payment_status="PENDIENTE"))

    assert result["motivoAnulacion"] == "STOCK_NO_DISPONIBLE"
    assert result["reembolso"] is None
    assert result["mensaje"] == "Tu pedido fue anulado por falta de stock."


def test_tracking_hides_a_free_text_reason_and_uses_the_generic_message():
    result = _tracking(CancelledSales(reason="Error interno: fallo en bodega 7"))

    assert result["motivoAnulacion"] is None
    assert result["mensaje"] == f"Tu pedido fue anulado. {REFUND}"
    assert "Error interno" not in str(result)


def test_tracking_of_a_cancelled_order_with_unknown_reason_uses_the_generic_message():
    result = _tracking(CancelledSales(reason="PAGO_NO_COMPLETADO", payment_status="PENDIENTE"))

    assert result["mensaje"] == "Tu pedido fue anulado."
    assert result["reembolso"] is None


def test_tracking_whitelist_keeps_the_cancellation_fields():
    from src.modules.mod_orders.domain.tracking_mapper import sanitize_tracking_payload

    clean = sanitize_tracking_payload(_tracking(CancelledSales()), pedido_id="PED-1", for_llm=True)

    assert clean["motivoAnulacion"] == "STOCK_NO_DISPONIBLE"
    assert clean["reembolso"] == REFUND


# -- Ownership (SPEC-17 RNF, backend#250) ---------------------------------------


class ForeignOwnerSales(G5ShapeSales):
    """Ventas answers a detail whose `contacto.clienteId` is another customer."""

    async def get_customer_order(self, order_id, holder_token, *, correlation_id=None):
        order = await super().get_customer_order(order_id, holder_token)
        order["contacto"]["clienteId"] = "99999999-9999-9999-9999-999999999999"
        return order


class NoContactSales(G5ShapeSales):
    """Detail without `contacto`; ownership can only come from the local order_ref."""

    async def get_customer_order(self, order_id, holder_token, *, correlation_id=None):
        order = await super().get_customer_order(order_id, holder_token)
        order.pop("contacto", None)
        return order


class FixedRefs:
    def __init__(self, customer_id, csat_sent=False):
        self._row = type("Ref", (), {"customer_id": customer_id, "csat_sent": csat_sent})()

    async def obtener_por_order_id(self, order_id):
        return self._row


def test_detail_of_a_foreign_order_is_404_like_an_unknown_one():
    service = OrdersService(notifications=None, sales_client=ForeignOwnerSales())  # type: ignore[arg-type]

    with pytest.raises(NotificationError) as exc:
        anyio.run(
            lambda: service.get_order_detail(
                G5_CUSTOMER_ID, "PED-2026-00001", holder_token="token"
            )
        )

    assert exc.value.code == "RECURSO_NO_ENCONTRADO"
    assert "No encontré ese pedido" in exc.value.detail


def test_detail_uses_order_ref_ownership_when_sales_omits_contact():
    service = OrdersService(
        notifications=None,
        sales_client=NoContactSales(),  # type: ignore[arg-type]
        order_refs=FixedRefs("99999999-9999-9999-9999-999999999999"),
    )

    with pytest.raises(NotificationError) as exc:
        anyio.run(
            lambda: service.get_order_detail(
                G5_CUSTOMER_ID, "PED-2026-00001", holder_token="token"
            )
        )

    assert exc.value.code == "RECURSO_NO_ENCONTRADO"


def test_tracking_of_a_foreign_order_is_404_like_an_unknown_one():
    service = OrdersService(notifications=None, sales_client=ForeignOwnerSales())  # type: ignore[arg-type]

    with pytest.raises(NotificationError) as exc:
        anyio.run(
            lambda: service.get_order_tracking(
                G5_CUSTOMER_ID, "PED-2026-00001", holder_token="token"
            )
        )

    assert exc.value.code == "RECURSO_NO_ENCONTRADO"


def test_detail_of_the_owner_keeps_working_with_matching_contact_and_order_ref():
    service = OrdersService(
        notifications=None,
        sales_client=G5ShapeSales(),  # type: ignore[arg-type]
        order_refs=FixedRefs(G5_CUSTOMER_ID, csat_sent=True),
    )

    result = anyio.run(
        lambda: service.get_order_detail(
            G5_CUSTOMER_ID, "PED-2026-00001", holder_token="token"
        )
    )

    assert result["estado"] == "ENTREGADO"
    assert result["csatEnviado"] is True
