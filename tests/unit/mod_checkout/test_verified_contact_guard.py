"""CheckoutGuard: the profile e-mail is the verified contact (fallback: verified phone)."""
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
PROFILE_EMAIL = "juan.perez@example.com"
PHONE = "+51987654321"


class FakeSecurityProfile:
    def __init__(self, profile):
        self.profile = profile

    async def me(self, token):
        return dict(self.profile)


class FakePhoneVerifications:
    def __init__(self, verified=False, verified_phone=PHONE):
        self.verified = verified
        self.verified_phone = verified_phone

    async def existe(self, customer_id, phone):
        return self.verified and phone == self.verified_phone


def build(
    *,
    require_phone=False,
    require_contact=True,
    phone_verified=False,
    profile=None,
):
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
        return_value=[
            SimpleNamespace(
                sku="SKU-1", name="Polo", quantity=1, unit_price_ref=50, image_url=None
            )
        ]
    )
    uow = MagicMock()
    uow.commit = AsyncMock()
    if profile is None:
        profile = {"correo": PROFILE_EMAIL, "celular": PHONE}
    return ManageCheckoutUseCase(
        checkouts=checkouts,
        payment_attempts=MagicMock(),
        payment_gateway=PaymentSimulatorAdapter(),
        uow=uow,
        security_client=FakeSecurityProfile(profile),
        carts=carts,
        cart_items=items,
        phone_verifications=FakePhoneVerifications(phone_verified),
        dispatch=FakeDispatch(),
        require_phone_verification=require_phone,
        require_verified_contact=require_contact,
    )


def payload(email=None):
    contacto = dict(CONTACT)
    if email is not None:
        contacto["email"] = email
    return CreateCheckoutInput(
        customer_id=CUSTOMER,
        idempotency_key="key-1",
        cart_id=CART,
        direccion=ADDRESS,
        contacto=contacto,
        access_token="token",
    )


@pytest.mark.asyncio
async def test_profile_email_lets_the_checkout_continue_without_a_phone_otp():
    use_case = build(phone_verified=False)
    row = await use_case.create_checkout(payload())
    assert row.customer_id == CUSTOMER
    assert row.summary["contacto"]["email"] == PROFILE_EMAIL


@pytest.mark.asyncio
async def test_without_profile_email_a_locally_verified_phone_is_enough():
    use_case = build(profile={"celular": PHONE}, phone_verified=True)
    row = await use_case.create_checkout(payload())
    assert row.customer_id == CUSTOMER
    assert "email" not in row.summary["contacto"]


@pytest.mark.asyncio
async def test_without_profile_email_or_verified_phone_the_checkout_is_blocked():
    use_case = build(profile={"celular": PHONE}, phone_verified=False)
    with pytest.raises(CheckoutError) as exc:
        await use_case.create_checkout(payload())
    assert exc.value.code == "CONTACTO_NO_VERIFICADO"
    assert exc.value.extra["motivo"] == "SIN_CONTACTO_VERIFICADO"
    assert exc.value.extra["celularEnmascarado"] == "+51 9****4321"


@pytest.mark.asyncio
async def test_legacy_phone_flag_wins_when_both_flags_are_on():
    use_case = build(require_phone=True, require_contact=True, phone_verified=False)
    with pytest.raises(CheckoutError) as exc:
        await use_case.create_checkout(payload())
    assert exc.value.code == "CELULAR_NO_VERIFICADO"


@pytest.mark.asyncio
async def test_seguridad_unavailable_blocks_without_a_verified_contact():
    use_case = build()

    async def unavailable(token):
        raise TimeoutError("security down")

    use_case.security_client = MagicMock(me=unavailable)
    with pytest.raises(CheckoutError) as exc:
        await use_case.create_checkout(payload())
    assert exc.value.code == "CONTACTO_NO_VERIFICADO"
    assert exc.value.extra["motivo"] == "SEGURIDAD_NO_DISPONIBLE"


@pytest.mark.asyncio
async def test_both_flags_off_keep_the_checkout_unguarded():
    use_case = build(require_phone=False, require_contact=False)
    use_case.security_client = None
    row = await use_case.create_checkout(payload())
    assert row.customer_id == CUSTOMER


@pytest.mark.asyncio
async def test_payload_email_is_replaced_by_the_profile_email():
    use_case = build()
    row = await use_case.create_checkout(payload(email="otro@example.com"))
    assert row.summary["contacto"]["email"] == PROFILE_EMAIL


@pytest.mark.asyncio
async def test_missing_session_is_reported_as_sin_sesion():
    use_case = build()
    data = payload()
    data.access_token = None
    with pytest.raises(CheckoutError) as exc:
        await use_case.create_checkout(data)
    assert exc.value.code == "CONTACTO_NO_VERIFICADO"
    assert exc.value.extra["motivo"] == "SIN_SESION"


@pytest.mark.asyncio
async def test_profile_without_any_contact_has_no_masked_phone():
    use_case = build(profile={})
    with pytest.raises(CheckoutError) as exc:
        await use_case.create_checkout(payload())
    assert exc.value.code == "CONTACTO_NO_VERIFICADO"
    assert exc.value.extra == {"motivo": "SIN_CONTACTO_VERIFICADO"}
