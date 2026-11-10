"""Cart, cart_item and checkout adapters against PostgreSQL 18.

Cubre el SQL que los tests de caso de uso no ejercitan: índices únicos
parciales (un carrito vivo por dueño, un checkout vivo por carrito),
idempotencia del checkout, topes del CHECK de intentos y el snapshot de
documento que consume la pantalla de cuenta.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from src.adapters.outbound.persistence.cart_item_postgres_adapter import (
    CartItemPostgresAdapter,
)
from src.adapters.outbound.persistence.cart_postgres_adapter import CartPostgresAdapter
from src.adapters.outbound.persistence.checkout_postgres_adapter import (
    CheckoutPostgresAdapter,
)
from src.adapters.outbound.persistence.models import Checkout, Conversation
from src.ports.outbound.cart_repository_port import CartConflictError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

AHORA = datetime.now(timezone.utc)


def nuevo_checkout(cart_id, customer_id, key, **extra) -> Checkout:
    datos = {
        "cart_id": cart_id,
        "customer_id": customer_id,
        "summary": {"lineas": [], "contacto": {}},
        "total": 35.5,
        "expires_at": AHORA + timedelta(minutes=15),
        "idempotency_key": key,
    }
    datos.update(extra)
    return Checkout(**datos)


async def test_carrito_de_cliente_es_unico_y_se_busca_por_dueno(postgres_session):
    adapter = CartPostgresAdapter(postgres_session)
    cliente = uuid.uuid4()

    carrito = await adapter.crear_para_cliente(cliente)
    assert carrito.status == "ACTIVE"

    assert (await adapter.obtener_por_id(carrito.id)).status == "ACTIVE"
    assert (await adapter.obtener_abierto_por_cliente(cliente)).id == carrito.id
    assert await adapter.obtener_abierto_por_cliente(uuid.uuid4()) is None

    with pytest.raises(CartConflictError):
        await adapter.crear_para_cliente(cliente)


async def test_carrito_anonimo_por_conversacion(postgres_session):
    conversacion = Conversation(anonymous_sid=f"sid-{uuid.uuid4().hex}")
    postgres_session.add(conversacion)
    await postgres_session.flush()

    adapter = CartPostgresAdapter(postgres_session)
    carrito = await adapter.crear_anonimo(conversacion.id)

    assert carrito.customer_id is None
    assert (await adapter.obtener_abierto_por_conversacion(conversacion.id)).id == carrito.id
    assert await adapter.obtener_abierto_por_conversacion(uuid.uuid4()) is None

    # Índice único parcial: un segundo carrito abierto en la misma conversación choca.
    with pytest.raises(CartConflictError):
        await adapter.crear_anonimo(conversacion.id)


async def test_estado_totales_cupon_y_reset(postgres_session):
    adapter = CartPostgresAdapter(postgres_session)
    carrito = await adapter.crear_para_cliente(uuid.uuid4())

    assert (await adapter.cambiar_estado(carrito.id, "IN_CHECKOUT")).status == "IN_CHECKOUT"
    assert await adapter.cambiar_estado(uuid.uuid4(), "ACTIVE") is None

    await adapter.set_totals(carrito.id, {"subtotal": "10.00", "total": "15.00"})
    await adapter.set_coupon(carrito.id, "VERANO10")
    guardado = await adapter.obtener_por_id(carrito.id)
    assert guardado.totals_snapshot == {"subtotal": "10.00", "total": "15.00"}
    assert guardado.coupon_code == "VERANO10"

    antes = guardado.updated_at
    await adapter.touch(carrito.id)
    assert (await adapter.obtener_por_id(carrito.id)).updated_at >= antes

    await adapter.reset_checkout(carrito.id)
    limpio = await adapter.obtener_por_id(carrito.id)
    assert limpio.coupon_code is None
    assert limpio.totals_snapshot is None


async def test_lineas_del_carrito_agregan_recalculan_y_borran(postgres_session):
    carrito = await CartPostgresAdapter(postgres_session).crear_para_cliente(uuid.uuid4())
    items = CartItemPostgresAdapter(postgres_session)

    linea = await items.agregar(
        cart_id=carrito.id,
        product_id="prod-1",
        sku="SKU-42-NEGRO",
        name="Zapatilla Volta",
        variant_description="42 · Negro",
        image_url=None,
        quantity=1,
        unit_price_ref=299.0,
    )
    assert (await items.obtener_por_id(linea.id)).sku == "SKU-42-NEGRO"
    assert (await items.obtener_por_cart_y_sku(carrito.id, "SKU-42-NEGRO")).id == linea.id
    assert await items.obtener_por_cart_y_sku(carrito.id, "OTRO") is None

    await items.agregar(
        cart_id=carrito.id,
        product_id="prod-2",
        sku="SKU-43-BLANCO",
        name="Zapatilla Volta",
        variant_description="43 · Blanco",
        image_url="https://img/43.png",
        quantity=2,
        unit_price_ref=279.0,
    )
    lineas = await items.listar_por_cart(carrito.id)
    assert [linea.sku for linea in lineas] == ["SKU-42-NEGRO", "SKU-43-BLANCO"]
    segunda = lineas[1]

    await items.set_quantity(linea.id, quantity=3, unit_price_ref=289.0)
    actualizada = await items.obtener_por_id(linea.id)
    assert (actualizada.quantity, float(actualizada.unit_price_ref)) == (3, 289.0)

    assert await items.eliminar(linea.id) is True
    assert await items.eliminar(linea.id) is False
    restantes = await items.listar_por_cart(carrito.id)
    assert [item.sku for item in restantes] == ["SKU-43-BLANCO"]

    # Al final: cantidad fuera del CHECK. Un flush fallido deja la sesión en
    # error, así que no puede haber más trabajo en esta transacción.
    with pytest.raises(CartConflictError):
        await items.set_quantity(segunda.id, quantity=99, unit_price_ref=289.0)


async def test_agregar_el_mismo_sku_dos_veces_choca_con_el_unico(postgres_session):
    carrito = await CartPostgresAdapter(postgres_session).crear_para_cliente(uuid.uuid4())
    items = CartItemPostgresAdapter(postgres_session)
    datos = dict(
        cart_id=carrito.id,
        product_id="prod-1",
        sku="SKU-42-NEGRO",
        name="Zapatilla Volta",
        variant_description=None,
        image_url=None,
        quantity=1,
        unit_price_ref=299.0,
    )

    await items.agregar(**datos)
    with pytest.raises(CartConflictError):
        await items.agregar(**datos)


async def test_checkout_idempotente_y_unico_por_carrito(postgres_session):
    carrito = await CartPostgresAdapter(postgres_session).crear_para_cliente(uuid.uuid4())
    cart_id = carrito.id  # el rollback del adapter expira las instancias ORM
    cliente = uuid.uuid4()
    adapter = CheckoutPostgresAdapter(postgres_session)

    primero = await adapter.guardar(nuevo_checkout(cart_id, cliente, "key-1"))
    primero_id = primero.id
    assert primero_id is not None
    # Cada intento del cliente es su propia transacción; se emula liberando el
    # savepoint para que el rollback del adapter no borre el checkout previo.
    await postgres_session.commit()

    # Misma clave: devuelve el mismo checkout, no crea otro.
    repetido = await adapter.guardar(nuevo_checkout(cart_id, cliente, "key-1"))
    assert repetido.id == primero_id

    # Otra clave, mismo carrito vivo: devuelve el existente (anti doble-clic).
    otro = await adapter.guardar(nuevo_checkout(cart_id, cliente, "key-2"))
    assert otro.id == primero_id

    # Otro dueño con el carrito de alguien más: no se filtra el checkout ajeno.
    with pytest.raises(IntegrityError):
        await adapter.guardar(nuevo_checkout(cart_id, uuid.uuid4(), "key-3"))


async def test_checkout_consultas_estado_e_intentos(postgres_session):
    carrito = await CartPostgresAdapter(postgres_session).crear_para_cliente(uuid.uuid4())
    cliente = uuid.uuid4()
    adapter = CheckoutPostgresAdapter(postgres_session)

    checkout = await adapter.guardar(nuevo_checkout(carrito.id, cliente, "key-1"))
    assert (await adapter.obtener_por_id(checkout.id)).id == checkout.id
    assert (await adapter.obtener_por_idempotency_key("key-1")).id == checkout.id
    assert await adapter.obtener_por_idempotency_key("nada") is None
    assert (await adapter.obtener_vivo_por_cart(carrito.id)).id == checkout.id
    assert (await adapter.obtener_vivo_por_cart(carrito.id, customer_id=cliente)).id == checkout.id
    assert await adapter.obtener_vivo_por_cart(carrito.id, customer_id=uuid.uuid4()) is None

    assert await adapter.marcar_estado(uuid.uuid4(), "CONFIRMED") is None
    assert (await adapter.marcar_estado(checkout.id, "CONFIRMED")).status == "CONFIRMED"
    # Ya no está vivo: el carrito puede abrir un checkout nuevo.
    assert await adapter.obtener_vivo_por_cart(carrito.id) is None

    assert await adapter.incrementar_intentos(uuid.uuid4()) is None
    for esperado in (1, 2, 3):
        assert (await adapter.incrementar_intentos(checkout.id)).payment_attempts == esperado

    # CHECK payment_attempts BETWEEN 0 AND 3: el cuarto intento lo rechaza la BD.
    # Deja la sesión en error, así que va al final del test.
    with pytest.raises(IntegrityError):
        await adapter.incrementar_intentos(checkout.id)


async def test_latest_document_usa_el_snapshot_del_ultimo_contacto(postgres_session):
    carrito = await CartPostgresAdapter(postgres_session).crear_para_cliente(uuid.uuid4())
    cart_id = carrito.id
    cliente = uuid.uuid4()
    adapter = CheckoutPostgresAdapter(postgres_session)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert await adapter.latest_document(cliente) is None

    sin_documento = await adapter.guardar(nuevo_checkout(cart_id, cliente, "key-1", created_at=t0))
    assert await adapter.latest_document(cliente) is None

    await adapter.marcar_estado(sin_documento.id, "FAILED")
    # Contacto incompleto (sin numeroDocumento) pero más reciente: no se expone.
    incompleto = await adapter.guardar(nuevo_checkout(
        cart_id,
        cliente,
        "key-2",
        created_at=t0 + timedelta(minutes=1),
        summary={"contacto": {"tipoDocumento": "DNI"}},
    ))
    assert await adapter.latest_document(cliente) is None

    # Un solo checkout vivo por carrito: el anterior pasa a FAILED antes del nuevo.
    await adapter.marcar_estado(incompleto.id, "FAILED")

    # El más reciente sí trae el documento completo.
    await adapter.guardar(nuevo_checkout(
        cart_id,
        cliente,
        "key-3",
        created_at=t0 + timedelta(minutes=2),
        summary={"contacto": {"tipoDocumento": "DNI", "numeroDocumento": "12345678"}},
    ))
    assert await adapter.latest_document(cliente) == {"tipo": "DNI", "numero": "12345678"}
    # Otro cliente no ve este documento.
    assert await adapter.latest_document(uuid.uuid4()) is None
