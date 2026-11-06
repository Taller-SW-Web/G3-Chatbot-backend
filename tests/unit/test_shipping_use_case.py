"""Shipping use case (`SPEC-12`): cart lines, saved addresses and forwarding.

The client never sends lines or prices: the use case reads the customer's
active cart and hands Despacho only `sku` + `cantidad`.
"""
import uuid

import pytest

from src.application.dto.commerce_contracts_dto import ShippingQuoteRequest
from src.application.use_cases.manage_shipping_use_case import (
    EMPTY_CART_CODE,
    ManageShippingUseCase,
)
from src.ports.outbound.module_errors import ModuleRequestError
from tests.fakes.fake_commerce import FakeCarts, FakeItems

CUSTOMER_ID = uuid.uuid4()
SKU = "NIK-REV7-42-NEG"


class _Dispatch:
    def __init__(self, result=None):
        self.result = result or {
            "coberturaDisponible": True,
            "costoEnvio": 12.5,
            "plazoEstimadoDiasHabiles": 1,
        }
        self.calls = []

    async def quote_shipping(self, *, destino, lineas, correlation_id=None):
        self.calls.append(
            {"destino": destino, "lineas": lineas, "correlation_id": correlation_id}
        )
        return self.result


class _Security:
    def __init__(self):
        self.calls = []

    async def list_addresses(self, user_id, holder_token, *, correlation_id=None):
        self.calls.append((user_id, holder_token))
        return [{"id": "addr-1", "distrito": "Miraflores", "direccion": "Av. Larco 1234"}]


def make_use_case(carts, items, dispatch, security):
    return ManageShippingUseCase(carts, items, dispatch, security)


async def _cart_with_line(carts, items, quantity=1):
    cart = await carts.crear_para_cliente(CUSTOMER_ID)
    await items.agregar(
        cart_id=cart.id,
        product_id="prod-1",
        sku=SKU,
        name="Zapatilla Runner",
        quantity=quantity,
        unit_price_ref=279.9,
    )
    return cart


def _payload(**overrides):
    data = {
        "destinatario": "Ana Díaz",
        "direccion": "Av. Larco 1234, dpto. 502",
        "distrito": "Miraflores",
        "referencia": "Frente al parque",
    }
    data.update(overrides)
    return ShippingQuoteRequest(**data)


@pytest.mark.asyncio
async def test_quote_reads_cart_lines_and_forwards_address():
    carts, items = FakeCarts(), FakeItems()
    await _cart_with_line(carts, items, quantity=2)
    dispatch = _Dispatch()
    use_case = make_use_case(carts, items, dispatch, _Security())

    result = await use_case.quote_shipping(CUSTOMER_ID, _payload())

    assert result["costoEnvio"] == 12.5
    call = dispatch.calls[0]
    assert call["lineas"] == [{"sku": SKU, "cantidad": 2}]
    assert call["destino"] == {
        "destinatario": "Ana Díaz",
        "direccion": "Av. Larco 1234, dpto. 502",
        "distrito": "Miraflores",
        "departamento": "Lima",
        "provincia": "Lima",
        "referencia": "Frente al parque",
    }
    # H4-S7: the service sends a dashed UUID as correlation id.
    assert str(uuid.UUID(call["correlation_id"])) == call["correlation_id"]


@pytest.mark.asyncio
async def test_quote_without_lines_fails_before_calling_dispatch():
    carts, items = FakeCarts(), FakeItems()
    await carts.crear_para_cliente(CUSTOMER_ID)
    dispatch = _Dispatch()
    use_case = make_use_case(carts, items, dispatch, _Security())

    with pytest.raises(ModuleRequestError) as exc:
        await use_case.quote_shipping(CUSTOMER_ID, _payload())

    assert exc.value.code == EMPTY_CART_CODE
    assert exc.value.status == 400
    assert dispatch.calls == []


@pytest.mark.asyncio
async def test_quote_without_an_active_cart_also_fails():
    carts, items = FakeCarts(), FakeItems()
    dispatch = _Dispatch()
    use_case = make_use_case(carts, items, dispatch, _Security())

    with pytest.raises(ModuleRequestError) as exc:
        await use_case.quote_shipping(CUSTOMER_ID, _payload())

    assert exc.value.code == EMPTY_CART_CODE
    assert dispatch.calls == []


@pytest.mark.asyncio
async def test_saved_addresses_are_proxied_from_seguridad():
    security = _Security()
    use_case = make_use_case(FakeCarts(), FakeItems(), _Dispatch(), security)

    addresses = await use_case.list_addresses(CUSTOMER_ID, holder_token="holder-token")

    assert addresses[0]["distrito"] == "Miraflores"
    assert security.calls == [(str(CUSTOMER_ID), "holder-token")]
