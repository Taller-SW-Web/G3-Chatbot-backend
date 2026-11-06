"""Cupones del carrito (`SPEC-13`): aplicar, reemplazar, quitar y límite de fallos.

Sin red ni base de datos: los repositorios y Productos son fakes. El caso de uso
valida el cupón contra `evaluar_promociones` (sin consumirlo) y persiste el
código; el consumo ocurre recién cuando Ventas confirma el pedido.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from src.adapters.inbound.http.dependencies.caller_identity import get_caller_identity
from src.application.services.coupon_attempts import LIMITE_FALLOS, CouponAttempts
from src.application.use_cases.manage_cart_use_case import CartError, ManageCartUseCase
from src.domain.value_objects.caller_identity import CallerIdentity
from src.infrastructure.config.container import provide_cart_service
from src.infrastructure.main import create_app
from tests.fakes.fake_commerce import (
    FakeCartRow,
    FakeCarts,
    FakeCatalogPort,
    FakeConversations,
    FakeInventoryPort,
    FakeItems,
    FakeMessages,
    FakeRow,
    FakeUow,
)

CUSTOMER_ID = uuid.uuid4()
SKU = "NIK-REV7-42-NEG"
PRECIO = 279.9
CUPON = "SPORT10"  # el fake de Productos descuenta 10 %
DESCUENTO = round(PRECIO * 0.1, 2)


def make_use_case(attempts=None):
    carts, items = FakeCarts(), FakeItems()
    use_case = ManageCartUseCase(
        carts,
        items,
        FakeCatalogPort(),
        FakeInventoryPort({SKU: "DISPONIBLE"}),
        FakeConversations(),
        FakeMessages(),
        FakeUow(),
        coupon_attempts=attempts,
    )
    return use_case, carts, items


async def con_linea(carts, items):
    cart = await carts.crear_para_cliente(CUSTOMER_ID)
    await items.agregar(
        cart_id=cart.id,
        product_id="prod-1",
        sku=SKU,
        name="Zapatilla Runner",
        quantity=1,
        unit_price_ref=PRECIO,
    )
    return cart


def caller() -> CallerIdentity:
    return CallerIdentity(customer_id=CUSTOMER_ID)


@pytest.mark.asyncio
async def test_aplica_un_cupon_valido_y_recalcula_el_total():
    use_case, carts, items = make_use_case()
    cart = await con_linea(carts, items)

    resultado = await use_case.apply_coupon(caller(), None, codigo=" sport10 ")

    assert resultado["cupon"]["codigo"] == CUPON
    datos = resultado["bloques"][0]["datos"]
    descuento = resultado["cupon"]["descuento"]
    assert descuento > 0
    assert datos["descuentos"] == [{"nombre": f"Cupón {CUPON}", "monto": descuento}]
    assert datos["total"] == pytest.approx(datos["subtotal"] - descuento, abs=0.01)
    assert carts.rows[cart.id].coupon_code == CUPON


@pytest.mark.asyncio
async def test_rechaza_un_cupon_invalido_con_motivo():
    use_case, carts, items = make_use_case()
    await con_linea(carts, items)

    with pytest.raises(CartError) as exc:
        await use_case.apply_coupon(caller(), None, codigo="NO-EXISTE")

    assert exc.value.code == "CUPON_INVALIDO"
    assert exc.value.data["motivo"] == "INEXISTENTE"


@pytest.mark.asyncio
async def test_reemplazar_exige_confirmacion():
    use_case, carts, items = make_use_case()
    cart = await con_linea(carts, items)
    await use_case.apply_coupon(caller(), None, codigo=CUPON)

    with pytest.raises(CartError) as exc:
        await use_case.apply_coupon(caller(), None, codigo="OTRO10")

    assert exc.value.code == "REQUIERE_CONFIRMACION_REEMPLAZO"
    assert exc.value.data == {"cuponActual": CUPON, "cuponNuevo": "OTRO10"}
    assert carts.rows[cart.id].coupon_code == CUPON

    # Con confirmación, el cupón se reemplaza (el fake solo conoce SPORT10).
    reemplazado = await use_case.apply_coupon(
        caller(), None, codigo="SPORT10", confirmar_reemplazo=True
    )
    assert reemplazado["cupon"]["codigo"] == CUPON


@pytest.mark.asyncio
async def test_quitar_el_cupon_recalcula_sin_descuento():
    use_case, carts, items = make_use_case()
    cart = await con_linea(carts, items)
    await use_case.apply_coupon(caller(), None, codigo=CUPON)

    resultado = await use_case.remove_coupon(caller(), None)

    assert resultado["cupon"] is None
    datos = resultado["bloques"][0]["datos"]
    assert datos["descuentos"] == []
    assert datos["total"] == pytest.approx(datos["subtotal"], abs=0.01)
    assert carts.rows[cart.id].coupon_code is None


@pytest.mark.asyncio
async def test_limita_los_intentos_fallidos_por_cliente():
    reloj = [1_000.0]
    attempts = CouponAttempts(clock=lambda: reloj[0])
    use_case, carts, items = make_use_case(attempts)
    await con_linea(carts, items)

    for _ in range(LIMITE_FALLOS):
        with pytest.raises(CartError) as exc:
            await use_case.apply_coupon(caller(), None, codigo="NO-EXISTE")
        assert exc.value.code == "CUPON_INVALIDO"

    with pytest.raises(CartError) as exc:
        await use_case.apply_coupon(caller(), None, codigo=CUPON)
    assert exc.value.code == "DEMASIADAS_SOLICITUDES"
    assert exc.value.data["reintentarEn"] >= 1

    # Pasada la ventana, el cliente puede volver a intentar.
    reloj[0] += 601
    assert await attempts.blocked(str(CUSTOMER_ID)) is None


@pytest.mark.asyncio
async def test_un_cupon_aplicado_limpia_los_fallos_previos():
    attempts = CouponAttempts()
    use_case, carts, items = make_use_case(attempts)
    await con_linea(carts, items)

    with pytest.raises(CartError):
        await use_case.apply_coupon(caller(), None, codigo="NO-EXISTE")
    await use_case.apply_coupon(caller(), None, codigo=CUPON)

    assert await attempts.blocked(str(CUSTOMER_ID)) is None


@pytest.mark.asyncio
async def test_requiere_sesion_de_cliente():
    use_case, carts, items = make_use_case()
    await con_linea(carts, items)

    with pytest.raises(CartError) as exc:
        await use_case.apply_coupon(None, None, codigo=CUPON)

    assert exc.value.code == "REQUIERE_SESION"


@pytest.mark.asyncio
async def test_carrito_vacio_no_acepta_cupon():
    use_case, _, _ = make_use_case()

    with pytest.raises(CartError) as exc:
        await use_case.apply_coupon(caller(), None, codigo=CUPON)

    assert exc.value.code == "RECURSO_NO_ENCONTRADO"


# -- contrato HTTP (router del H4-A1) ----------------------------------------


def make_app():
    use_case, carts, items = make_use_case(CouponAttempts())
    app = create_app()
    app.dependency_overrides[provide_cart_service] = lambda: use_case
    app.dependency_overrides[get_caller_identity] = lambda: caller()
    return TestClient(app), carts, items


def seed_cart(carts, items):
    """Carrito con una línea, sin pasar por el caso de uso (setup síncrono)."""
    cart = FakeCartRow(customer_id=CUSTOMER_ID)
    carts.rows[cart.id] = cart
    item = FakeRow(
        cart_id=cart.id,
        product_id="prod-1",
        sku=SKU,
        name="Zapatilla Runner",
        variant_description=None,
        image_url=None,
        quantity=1,
        unit_price_ref=PRECIO,
    )
    items.rows[item.id] = item


def test_router_aplica_y_quita_el_cupon():
    client, carts, items = make_app()
    seed_cart(carts, items)

    aplicado = client.post("/api/v1/carrito/cupon", json={"codigo": "sport10"})
    assert aplicado.status_code == 200, aplicado.text
    cuerpo = aplicado.json()
    assert cuerpo["cupon"]["codigo"] == CUPON
    assert cuerpo["carrito"]["descuentos"][0]["nombre"] == f"Cupón {CUPON}"

    quitado = client.delete("/api/v1/carrito/cupon")
    assert quitado.status_code == 200, quitado.text
    assert quitado.json()["carrito"]["descuentos"] == []


def test_router_traduce_el_cupon_invalido_y_el_reemplazo():
    client, carts, items = make_app()
    seed_cart(carts, items)

    invalido = client.post("/api/v1/carrito/cupon", json={"codigo": "NO-EXISTE"})
    assert invalido.status_code == 422
    assert invalido.headers["content-type"].startswith("application/problem+json")
    assert invalido.json()["code"] == "CUPON_INVALIDO"
    assert invalido.json()["motivo"] == "INEXISTENTE"

    assert client.post("/api/v1/carrito/cupon", json={"codigo": CUPON}).status_code == 200
    reemplazo = client.post("/api/v1/carrito/cupon", json={"codigo": "OTRO10"})
    assert reemplazo.status_code == 409
    assert reemplazo.json()["code"] == "REQUIERE_CONFIRMACION_REEMPLAZO"
    assert reemplazo.json()["cuponActual"] == CUPON
