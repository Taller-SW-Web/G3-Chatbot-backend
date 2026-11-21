"""Chat order status (SPEC-17): list, detail, not found and degraded Ventas."""
import base64
import json
import uuid
from typing import Any

import pytest

from src.application.tools.order_handlers import build_order_handlers
from src.modules.mod_catalog.application.tools import OrderQueryArguments, build_tool_registry
from src.modules.mod_orders.application.orders_service import OrdersService
from src.shared.application.tools.tool_registry import ExecutionContext
from src.shared.domain.module_errors import ModuleUnavailableError
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


def _snapshot(customer_id: uuid.UUID = CUSTOMER_ID, total: float = 112.4) -> dict[str, Any]:
    return {
        "canal": "CHATBOT",
        "contacto": {"clienteId": str(customer_id)},
        "items": [{"productoId": "P-1", "sku": "SKU-1", "descripcion": "Zapatilla",
                   "cantidad": 1, "precioUnitario": 99.9}],
        "envio": {"distrito": "Miraflores", "costo": 12.5},
        "pago": {"total": total},
    }


def _handler(sales: FakeSalesPort | None = None, *, unavailable: bool = False):
    if unavailable:
        class Down:
            async def list_orders(self, *args, **kwargs):
                raise ModuleUnavailableError("sales")

            async def get_order_detail(self, *args, **kwargs):
                raise ModuleUnavailableError("sales")

        return build_order_handlers(Down())["consultar_pedidos"]
    service = OrdersService(notifications=None, sales_client=sales)  # type: ignore[arg-type]
    return build_order_handlers(service)["consultar_pedidos"]


async def _order(sales: FakeSalesPort, *, key: str = "k1",
                 customer_id: uuid.UUID = CUSTOMER_ID, total: float = 112.4) -> str:
    created = await sales.create_order(_snapshot(customer_id, total), idempotency_key=key)
    return str(created["pedidoId"])


@pytest.mark.asyncio
async def test_spec_17_req1_single_order_is_shown_directly():
    sales = FakeSalesPort()
    order_id = await _order(sales)
    result = await _handler(sales)(OrderQueryArguments(), _context())
    assert result.code == "OK"
    assert order_id in result.data["texto"]
    block = result.data["bloques"][0]
    assert block["tipo"] == "ESTADO_PEDIDO"
    assert block["datos"]["pedido"]["pedidoId"] == order_id
    assert block["datos"]["pedido"]["estadoEtiqueta"] == "Pendiente de pago"
    assert [hito["titulo"] for hito in block["datos"]["hitos"]][:2] == [
        "Pendiente de pago", "Pago confirmado"]


@pytest.mark.asyncio
async def test_spec_17_req1_several_orders_are_listed_to_choose():
    sales = FakeSalesPort()
    await _order(sales, key="k1")
    await _order(sales, key="k2", total=50.0)
    result = await _handler(sales)(OrderQueryArguments(), _context())
    block = result.data["bloques"][0]
    assert block["tipo"] == "LISTA_PEDIDOS"
    pedidos = block["datos"]["pedidos"]
    assert len(pedidos) == 2
    for row in pedidos:
        assert {"pedidoId", "fecha", "total", "estado", "estadoEtiqueta", "canal"} <= set(row)
    assert {row["total"] for row in pedidos} == {"S/ 112.40", "S/ 50.00"}


@pytest.mark.asyncio
async def test_spec_17_req1_state_filter_is_forwarded():
    sales = FakeSalesPort()
    await _order(sales, key="k1")
    result = await _handler(sales)(OrderQueryArguments(estado="ANULADO"), _context())
    assert "Aún no tienes pedidos" in result.data["texto"]
    assert result.data["bloques"][0]["datos"]["acciones"][0]["id"] == "VER_OFERTAS"


@pytest.mark.asyncio
async def test_spec_17_req1_order_by_number_returns_detail():
    sales = FakeSalesPort()
    order_id = await _order(sales, key="k1")
    await _order(sales, key="k2")
    result = await _handler(sales)(
        OrderQueryArguments(pedidoId=order_id), _context())
    assert result.data["bloques"][0]["tipo"] == "ESTADO_PEDIDO"
    assert result.data["bloques"][0]["datos"]["pedido"]["pedidoId"] == order_id
    assert result.data["bloques"][0]["datos"]["hitos"][0]["done"] is True


@pytest.mark.asyncio
async def test_spec_17_req1_unknown_or_foreign_order_says_not_found():
    sales = FakeSalesPort()
    await _order(sales, key="k1")
    foreign = await _order(sales, key="k2", customer_id=OTHER_CUSTOMER)
    handler = _handler(sales)
    for pedido_id in ("PED-2026-NOEXISTE", foreign):
        result = await handler(OrderQueryArguments(pedidoId=pedido_id), _context())
        assert "No encontré ese pedido en tu cuenta" in result.data["texto"]
        assert result.data["bloques"][0]["datos"]["acciones"][0]["id"] == "CONSULTAR_PEDIDOS"


@pytest.mark.asyncio
async def test_spec_17_req1_customer_without_orders_is_offered_offers():
    result = await _handler(FakeSalesPort())(OrderQueryArguments(), _context())
    assert "Aún no tienes pedidos" in result.data["texto"]


@pytest.mark.asyncio
async def test_spec_17_req1_requires_a_customer_session_with_holder_token():
    sales = FakeSalesPort()
    result = await _handler(sales)(OrderQueryArguments(), _context(customer_id=None))
    assert result.code == "REQUIERE_SESION"
    # A customer id without the bearer token cannot query Ventas either.
    anonymous = ExecutionContext(conversacion_id=str(uuid.uuid4()), cliente_id=str(CUSTOMER_ID))
    result = await _handler(sales)(OrderQueryArguments(), anonymous)
    assert result.code == "REQUIERE_SESION"


@pytest.mark.asyncio
async def test_spec_17_req4_ventas_down_offers_retry_without_inventing_status():
    result = await _handler(unavailable=True)(OrderQueryArguments(), _context())
    assert result.code == "OK"
    assert "No puedo consultar tus pedidos" in result.data["texto"]
    assert result.data["bloques"][0]["datos"]["acciones"][0]["etiqueta"] == "Reintentar"
    assert "CREADO" not in result.data["texto"]


@pytest.mark.asyncio
async def test_tool_is_registered_as_session_only():
    sales = FakeSalesPort()
    registry = build_tool_registry({"consultar_pedidos": _handler(sales)})
    definition = next(tool for tool in registry.catalogo() if tool["nombre"] == "consultar_pedidos")
    assert definition["requiere_sesion"] is True
    anonymous = await registry.ejecutar(
        "consultar_pedidos", {}, ExecutionContext(conversacion_id=str(uuid.uuid4())))
    assert anonymous.code == "REQUIERE_SESION"
