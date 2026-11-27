"""M5 order tools: D7 single-order rule and `consultar_seguimiento`."""

import base64
import json
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from src.modules.mod_catalog.application.tools import OrderQueryArguments, TrackingArguments
from src.modules.mod_chat.application.tools.order_handlers import build_order_handlers
from src.modules.mod_orders.application.orders_service import OrdersService
from src.modules.mod_orders.domain.tracking_mapper import sanitize_tracking_payload
from src.shared.application.tools.tool_registry import ExecutionContext
from src.shared.domain.module_errors import ModuleRequestError
from src.shared.infrastructure.fakes.fake_ventas import FakeSalesPort

CUSTOMER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
OTHER_CUSTOMER = uuid.UUID("99999999-8888-7777-6666-555555555555")


def _jwt(sub: uuid.UUID) -> str:
    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part({'sub': str(sub)})}.signature"


def _context(customer_id: uuid.UUID | None = CUSTOMER_ID, token: str | None = None):
    return ExecutionContext(
        conversacion_id=str(uuid.uuid4()),
        cliente_id=str(customer_id) if customer_id else None,
        access_token=token if token is not None else _jwt(customer_id or CUSTOMER_ID),
    )


def _snapshot(customer_id: uuid.UUID = CUSTOMER_ID) -> dict[str, Any]:
    return {
        "canal": "CHATBOT",
        "contacto": {"clienteId": str(customer_id)},
        "items": [{"productoId": "P-1", "sku": "SKU-1", "descripcion": "Zapatilla",
                   "cantidad": 1, "precioUnitario": 99.9}],
        "envio": {"distrito": "Miraflores", "costo": 12.5,
                  "fechaEstimadaEntrega": "2026-12-01"},
        "pago": {"total": 112.4},
    }


class TrackingDispatch:
    def __init__(self, response: Any = None, error: Exception | None = None):
        self.response = response
        self.error = error

    async def get_order_tracking(self, order_id: str, *, correlation_id: str | None = None):
        if self.error is not None:
            raise self.error
        return self.response


async def _order(sales: FakeSalesPort, *, key: str = "k1",
                 customer_id: uuid.UUID = CUSTOMER_ID) -> str:
    created = await sales.create_order(_snapshot(customer_id), idempotency_key=key)
    return str(created["pedidoId"])


def _handlers(sales: FakeSalesPort, dispatch: Any = None):
    service = OrdersService(notifications=None, sales_client=sales, dispatch=dispatch)  # type: ignore[arg-type]
    return build_order_handlers(service)


@pytest.mark.asyncio
async def test_a_lone_delivered_order_is_listed_not_duplicated_as_detail():
    sales = FakeSalesPort()
    order_id = await _order(sales)
    sales.orders[order_id]["estado"] = "ENTREGADO"
    result = await _handlers(sales)["consultar_pedidos"](OrderQueryArguments(), _context())
    assert result.code == "OK"
    (block,) = result.data["bloques"]
    assert block["tipo"] == "LISTA_PEDIDOS"
    assert [row["pedidoId"] for row in block["datos"]["pedidos"]] == [order_id]


@pytest.mark.asyncio
async def test_one_in_progress_plus_delivered_shows_the_in_progress_order():
    sales = FakeSalesPort()
    delivered = await _order(sales, key="k1")
    in_progress = await _order(sales, key="k2")
    sales.orders[delivered]["estado"] = "ENTREGADO"
    sales.orders[in_progress]["estado"] = "EN_PREPARACION"
    result = await _handlers(sales)["consultar_pedidos"](OrderQueryArguments(), _context())
    (block,) = result.data["bloques"]
    assert block["tipo"] == "ESTADO_PEDIDO"
    assert block["datos"]["pedido"]["pedidoId"] == in_progress


@pytest.mark.asyncio
async def test_two_in_progress_orders_are_listed():
    sales = FakeSalesPort()
    await _order(sales, key="k1")
    await _order(sales, key="k2")
    result = await _handlers(sales)["consultar_pedidos"](OrderQueryArguments(), _context())
    (block,) = result.data["bloques"]
    assert block["tipo"] == "LISTA_PEDIDOS"
    assert len(block["datos"]["pedidos"]) == 2


@pytest.mark.asyncio
async def test_explicit_state_never_uses_the_single_order_rule():
    sales = FakeSalesPort()
    order_id = await _order(sales)
    result = await _handlers(sales)["consultar_pedidos"](
        OrderQueryArguments(estado="CREADO"), _context())
    (block,) = result.data["bloques"]
    assert block["tipo"] == "LISTA_PEDIDOS"
    assert block["datos"]["pedidos"][0]["pedidoId"] == order_id


@pytest.mark.asyncio
async def test_tracking_adds_the_dispatch_timeline_next_to_the_sales_timeline():
    sales = FakeSalesPort()
    order_id = await _order(sales)
    sales.orders[order_id]["estado"] = "DESPACHADO"
    dispatch = TrackingDispatch({
        "pedidoId": order_id,
        "estado": "EN_CAMINO",
        "estadoEtiqueta": "En camino",
        "fechaProgramada": "2026-12-01",
        "distrito": "Miraflores",
        "recibidoPor": "Ana",
        "detalleDisponible": True,
        "mensaje": None,
        "hitos": [{"etiqueta": "En camino", "fecha": "2026-11-30T16:00:00Z", "completado": True}],
        "lat": -12.1,
        "driver": "interno",
    })
    result = await _handlers(sales, dispatch)["consultar_seguimiento"](
        TrackingArguments(pedidoId=order_id), _context())
    assert result.code == "OK"
    (block,) = result.data["bloques"]
    assert block["tipo"] == "ESTADO_PEDIDO"
    seguimiento = block["datos"]["seguimiento"]
    assert set(seguimiento) == {
        "pedidoId", "estado", "estadoEtiqueta", "fechaProgramada", "distrito",
        "hitos", "detalleDisponible", "mensaje", "recibidoPor",
    }
    assert seguimiento["recibidoPor"] == "Ana"
    assert seguimiento["hitos"] == [
        {"titulo": "En camino", "fecha": "2026-11-30T16:00:00Z", "completado": True}
    ]
    assert block["datos"]["hitos"][0]["titulo"] == "Pendiente de pago"
    assert "lat" not in str(block) and "driver" not in str(block)


def test_tracking_milestones_read_the_g2_etiqueta_field():
    """G2 publishes the milestone label as `etiqueta`; the BFF exposes `titulo`."""
    raw = {
        "pedidoId": "PED-2026-00001",
        "estado": "EN_CAMINO",
        "hitos": [
            {
                "estado": "EN_CAMINO",
                "etiqueta": "En camino",
                "fecha": "2026-11-30T16:00:00Z",
            }
        ],
    }

    seguimiento = sanitize_tracking_payload(raw)

    assert seguimiento["hitos"] == [
        {"titulo": "En camino", "fecha": "2026-11-30T16:00:00Z", "completado": True}
    ]


@pytest.mark.asyncio
async def test_tracking_404_from_dispatch_degrades_without_hiding_the_order():
    sales = FakeSalesPort()
    order_id = await _order(sales)
    sales.orders[order_id]["estado"] = "DESPACHADO"
    dispatch = TrackingDispatch(error=ModuleRequestError("dispatch", 404, "NO_ENCONTRADO"))
    result = await _handlers(sales, dispatch)["consultar_seguimiento"](
        TrackingArguments(pedidoRef=order_id), _context())
    assert result.code == "OK"
    (block,) = result.data["bloques"]
    seguimiento = block["datos"]["seguimiento"]
    assert seguimiento["detalleDisponible"] is False
    assert seguimiento["hitos"] == []
    assert block["datos"]["pedido"]["pedidoId"] == order_id


@pytest.mark.asyncio
async def test_tracking_requires_one_reference_and_a_session():
    with pytest.raises(ValidationError):
        TrackingArguments()
    with pytest.raises(ValidationError):
        TrackingArguments(pedidoId="P-1", pedidoRef="P-1")
    sales = FakeSalesPort()
    result = await _handlers(sales)["consultar_seguimiento"](
        TrackingArguments(pedidoId="P-1"), _context(customer_id=None))
    assert result.code == "REQUIERE_SESION"


@pytest.mark.asyncio
async def test_tracking_a_foreign_order_is_not_found():
    sales = FakeSalesPort()
    foreign = await _order(sales, customer_id=OTHER_CUSTOMER, key="foreign")
    result = await _handlers(sales)["consultar_seguimiento"](
        TrackingArguments(pedidoId=foreign), _context())
    assert result.code == "OK"
    assert "No encontré ese pedido en tu cuenta" in result.data["texto"]


@pytest.mark.asyncio
async def test_tracking_of_a_stock_cancelled_order_explains_reason_and_refund():
    sales = FakeSalesPort()
    order_id = await _order(sales)
    sales.cancel_for_stock(order_id)

    result = await _handlers(sales)["consultar_seguimiento"](TrackingArguments(pedidoId=order_id), _context())

    refund = "Se procesará el reembolso al medio de pago original."
    assert result.data["texto"] == f"Tu pedido fue anulado por falta de stock. {refund}"
    (block,) = result.data["bloques"]
    assert block["datos"]["pedido"]["motivoAnulacion"] == "STOCK_NO_DISPONIBLE"
    assert block["datos"]["pedido"]["reembolso"] == refund
    assert block["datos"]["seguimiento"]["motivoAnulacion"] == "STOCK_NO_DISPONIBLE"
