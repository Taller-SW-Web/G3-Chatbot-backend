"""Optional saved address at checkout (SPEC-12 Req. 3)."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.modules.mod_checkout.adapters.payment_simulator_adapter import PaymentSimulatorAdapter
from src.modules.mod_checkout.application.manage_checkout_use_case import (
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
ADDRESS = {"direccionExacta": "Av. Larco 1234", "distrito": "Miraflores", "departamento": "Lima"}


class FakeSecurity:
    def __init__(self, fail=False):
        self.fail = fail
        self.saved = []

    async def save_address(self, user_id, token, address):
        if self.fail:
            raise TimeoutError("seguridad down")
        self.saved.append((user_id, token, address))
        return {"id": "dir-1"}


def build(security, *, enabled=True):
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
        return_value=[SimpleNamespace(sku="SKU-1", name="Polo", quantity=1, unit_price_ref=50, image_url=None)]
    )
    uow = MagicMock()
    uow.commit = AsyncMock()
    return ManageCheckoutUseCase(
        checkouts=checkouts,
        payment_attempts=MagicMock(),
        payment_gateway=PaymentSimulatorAdapter(),
        uow=uow,
        security_client=security,
        carts=carts,
        cart_items=items,
        dispatch=FakeDispatch(),
        save_address_enabled=enabled,
    )


def payload(guardar=True):
    return CreateCheckoutInput(
        customer_id=CUSTOMER,
        idempotency_key="key-address",
        cart_id=CART,
        direccion=ADDRESS,
        contacto=CONTACT,
        access_token="token",
        guardar_direccion=guardar,
    )


@pytest.mark.asyncio
async def test_customer_marks_save_and_seguridad_receives_the_address():
    security = FakeSecurity()
    row = await build(security).create_checkout(payload())
    assert security.saved == [(str(CUSTOMER), "token", ADDRESS)]
    assert row.summary["avisoDireccion"] is None


@pytest.mark.asyncio
async def test_seguridad_failure_does_not_block_and_informs():
    security = FakeSecurity(fail=True)
    row = await build(security).create_checkout(payload())
    assert row.summary["avisoDireccion"] == "No pudimos guardar tu dirección para la próxima vez."


@pytest.mark.asyncio
async def test_unchecked_box_does_not_call_seguridad():
    security = FakeSecurity()
    await build(security).create_checkout(payload(guardar=False))
    assert security.saved == []


@pytest.mark.asyncio
async def test_flag_off_skips_the_save():
    security = FakeSecurity()
    row = await build(security, enabled=False).create_checkout(payload())
    assert security.saved == []
    assert row.summary["avisoDireccion"] is None
