"""CheckoutGuard: the checkout demands the current phone verified (SPEC-04)."""
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

CONTACT = {
    "nombreCompleto": "Juan Pérez Rodríguez",
    "tipoDocumento": "DNI",
    "numeroDocumento": "72458912",
}
ADDRESS = {"direccionExacta": "Av. Larco 1234", "distrito": "Miraflores"}
CUSTOMER = uuid.uuid4()
CART = uuid.uuid4()


class FakeSecurityProfile:
    def __init__(self, phone="+51987654321"):
        self.phone = phone

    async def me(self, token):
        return {"celular": self.phone}


class FakePhoneVerifications:
    def __init__(self, verified=False, verified_phone="+51987654321"):
        self.verified_phone = verified_phone
        self.verified = verified

    async def existe(self, customer_id, phone):
        return self.verified and phone == self.verified_phone


def build(require=True, verified=False, phone="+51987654321"):
    checkouts = MagicMock()
    checkouts.obtener_por_idempotency_key = AsyncMock(return_value=None)
    checkouts.obtener_vivo_por_cart = AsyncMock(return_value=None)
    checkouts.guardar = AsyncMock(side_effect=lambda row: row)
    carts = MagicMock()
    carts.obtener_por_id = AsyncMock(
        return_value=SimpleNamespace(id=CART, customer_id=CUSTOMER, totals_snapshot=None)
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
        security_client=FakeSecurityProfile(phone),
        carts=carts,
        cart_items=items,
        phone_verifications=FakePhoneVerifications(verified),
        dispatch=FakeDispatch(),
        require_phone_verification=require,
    )


def payload():
    return CreateCheckoutInput(
        customer_id=CUSTOMER,
        idempotency_key="key-1",
        cart_id=CART,
        direccion=ADDRESS,
        contacto=CONTACT,
        access_token="token",
    )


@pytest.mark.asyncio
async def test_unverified_current_phone_blocks_with_403_and_masked_number():
    use_case = build(require=True, verified=False)
    with pytest.raises(CheckoutError) as exc:
        await use_case.create_checkout(payload())
    assert exc.value.code == "CELULAR_NO_VERIFICADO"
    assert exc.value.extra["celularEnmascarado"] == "+51 9****4321"


@pytest.mark.asyncio
async def test_verified_current_phone_lets_the_checkout_continue():
    use_case = build(require=True, verified=True)
    row = await use_case.create_checkout(payload())
    assert row.customer_id == CUSTOMER


@pytest.mark.asyncio
async def test_changed_phone_needs_verification_again():
    use_case = build(require=True, verified=True, phone="+51911222333")
    with pytest.raises(CheckoutError) as exc:
        await use_case.create_checkout(payload())
    assert exc.value.code == "CELULAR_NO_VERIFICADO"
    assert exc.value.extra["celularEnmascarado"] == "+51 9****2333"


@pytest.mark.asyncio
async def test_flag_off_keeps_development_flow_working():
    use_case = build(require=False, verified=False)
    row = await use_case.create_checkout(payload())
    assert row.customer_id == CUSTOMER


@pytest.mark.asyncio
async def test_seguridad_unavailable_blocks_instead_of_skipping_verification():
    use_case = build(require=True, verified=True)

    async def unavailable(token):
        raise TimeoutError("security down")

    use_case.security_client = MagicMock(me=unavailable)
    with pytest.raises(CheckoutError) as exc:
        await use_case.create_checkout(payload())
    assert exc.value.code == "CELULAR_NO_VERIFICADO"
    assert exc.value.extra["motivo"] == "SEGURIDAD_NO_DISPONIBLE"
