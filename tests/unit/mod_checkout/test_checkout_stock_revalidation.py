"""Checkout revalidates the whole cart before quoting/creating the order (SPEC-10)."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.modules.mod_checkout.adapters.payment_simulator_adapter import PaymentSimulatorAdapter
from src.modules.mod_checkout.application.manage_checkout_use_case import (
    CheckoutError,
    CreateCheckoutInput,
    ManageCheckoutUseCase,
)
from tests.fakes.fake_commerce import FakeDispatch

CUSTOMER = uuid.uuid4()
CART = uuid.uuid4()
CONTACT = {
    "nombreCompleto": "Juan Pérez Rodríguez",
    "tipoDocumento": "DNI",
    "numeroDocumento": "72458912",
}
ADDRESS = {"direccionExacta": "Av. Larco 1234", "distrito": "Miraflores"}


def build(validator):
    checkouts = MagicMock()
    checkouts.obtener_por_idempotency_key = AsyncMock(return_value=None)
    checkouts.obtener_vivo_por_cart = AsyncMock(return_value=None)
    checkouts.guardar = AsyncMock(side_effect=lambda row: row)
    carts = MagicMock()
    carts.obtener_por_id = AsyncMock(
        return_value=SimpleNamespace(
            id=CART,
            customer_id=CUSTOMER,
            totals_snapshot={
                "subtotal": "50.00",
                "descuentos": [],
                "total": "50.00",
                "promociones_ok": True,
            },
        )
    )
    carts.cambiar_estado = AsyncMock()
    items = MagicMock()
    items.listar_por_cart = AsyncMock(
        return_value=[SimpleNamespace(sku="SKU-1", name="Zapatillas", quantity=1, unit_price_ref=50, image_url=None)]
    )
    uow = MagicMock()
    uow.commit = AsyncMock()
    return ManageCheckoutUseCase(
        checkouts=checkouts,
        payment_attempts=MagicMock(),
        payment_gateway=PaymentSimulatorAdapter(),
        uow=uow,
        carts=carts,
        cart_items=items,
        dispatch=FakeDispatch(),
        cart_validator=validator,
    )


def payload():
    return CreateCheckoutInput(
        customer_id=CUSTOMER,
        idempotency_key="key-stock",
        cart_id=CART,
        direccion=ADDRESS,
        contacto=CONTACT,
    )


@pytest.mark.asyncio
async def test_out_of_stock_line_returns_409_with_per_line_detail():
    async def validator(cart):
        return {"SKU-1": {"projection": "SKU_AGOTADO", "quantity": "OK", "ok": False}}

    use_case = build(validator)
    with pytest.raises(CheckoutError) as exc:
        await use_case.create_checkout(payload())

    assert exc.value.code == "CARRITO_DESACTUALIZADO"
    assert exc.value.extra["lineas"] == [
        {
            "sku": "SKU-1",
            "nombre": "Zapatillas",
            "cantidad": 1,
            "motivo": "AGOTADO",
            "mensaje": "Zapatillas se agotó mientras elegías.",
        }
    ]


@pytest.mark.asyncio
async def test_insufficient_quantity_line_returns_409():
    async def validator(cart):
        return {"SKU-1": {"projection": "STOCK_BAJO", "quantity": "STOCK_INSUFICIENTE", "ok": False}}

    use_case = build(validator)
    with pytest.raises(CheckoutError) as exc:
        await use_case.create_checkout(payload())
    assert exc.value.extra["lineas"][0]["motivo"] == "STOCK_INSUFICIENTE"


@pytest.mark.asyncio
async def test_indeterminate_stock_does_not_block_the_checkout():
    async def validator(cart):
        return {"SKU-1": {"projection": "INDETERMINADO", "quantity": "INDETERMINADO", "ok": True}}

    use_case = build(validator)
    row = await use_case.create_checkout(payload())
    assert row.customer_id == CUSTOMER


@pytest.mark.asyncio
async def test_validator_unavailable_blocks_instead_of_assuming_stock():
    async def validator(cart):
        raise TimeoutError("inventory down")

    use_case = build(validator)
    with pytest.raises(CheckoutError) as exc:
        await use_case.create_checkout(payload())
    assert exc.value.code == "MODULO_NO_DISPONIBLE"
