"""Regression tests for issue #180: no orphan Ventas orders on a checkout timeout.

The fake Ventas creates the order *before* sleeping, exactly like the real
service did in the 2026-11-26 E2E: the client times out, but `PED-...` exists.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.modules.mod_checkout.application.manage_checkout_use_case import (
    SALES_PENDING_KEY,
    SALES_SNAPSHOT_KEY,
    CheckoutError,
    CreateCheckoutInput,
    ManageCheckoutUseCase,
    ProcessPaymentInput,
)
from src.modules.mod_checkout.domain.checkout import CheckoutStatus
from src.shared.domain.module_errors import ModuleRequestError
from tests.fakes.fake_commerce import FakeDispatch, FakeUow

CUSTOMER = uuid.uuid4()
CART = uuid.uuid4()
CONTACT = {
    "nombreCompleto": "Juan Pérez Rodríguez",
    "tipoDocumento": "DNI",
    "numeroDocumento": "72458912",
    "telefono": "+51999888777",
    "email": "juan@example.com",
}
ADDRESS = {
    "direccionExacta": "Av. Larco 1234",
    "distrito": "Miraflores",
    "departamento": "Lima",
    "provincia": "Lima",
}


class FakeSalesPort:
    """Ventas fake: creates the order first, then answers (or times out)."""

    def __init__(self, *, delay: float = 0.0, error: Exception | None = None):
        self.delay = delay
        self.error = error
        self.orders: dict[str, str] = {}
        self.calls: list[str] = []

    async def create_order(
        self, snapshot, *, idempotency_key: str, correlation_id: str | None = None
    ):
        self.calls.append(idempotency_key)
        if self.error is not None:
            raise self.error
        order_id = self.orders.setdefault(idempotency_key, f"PED-TEST-{len(self.orders) + 1}")
        if self.delay:
            await asyncio.sleep(self.delay)
        return {"pedidoId": order_id, "estado": "CREADO"}


class InMemoryCheckouts:
    def __init__(self):
        self.by_id: dict[uuid.UUID, object] = {}
        self.by_key: dict[str, object] = {}

    async def guardar(self, row):
        existing = self.by_key.get(row.idempotency_key)
        if existing is not None and existing.id != row.id:
            return existing
        live = await self.obtener_vivo_por_cart(row.cart_id, customer_id=row.customer_id)
        if live is not None and live.id != row.id:
            return live
        self.by_id[row.id] = row
        self.by_key[row.idempotency_key] = row
        return row

    async def obtener_por_id(self, checkout_id, *, for_update: bool = False):
        return self.by_id.get(checkout_id)

    async def obtener_por_idempotency_key(self, idempotency_key):
        return self.by_key.get(idempotency_key)

    async def obtener_vivo_por_cart(self, cart_id, customer_id=None):
        for row in self.by_id.values():
            if row.cart_id != cart_id:
                continue
            if row.status not in (
                CheckoutStatus.PENDING_PAYMENT.value,
                CheckoutStatus.PAYMENT_APPROVED.value,
            ):
                continue
            if customer_id is not None and row.customer_id != customer_id:
                continue
            return row
        return None

    async def marcar_estado(self, checkout_id, status):
        row = self.by_id.get(checkout_id)
        if row is not None:
            row.status = status
        return row


class FakeCarts:
    def __init__(self):
        self.row = SimpleNamespace(
            id=CART,
            customer_id=CUSTOMER,
            status="ACTIVE",
            totals_snapshot={
                "subtotal": "100.00",
                "descuentos": [],
                "total": "100.00",
                "promociones_ok": True,
            },
        )

    async def obtener_por_id(self, cart_id):
        return self.row if cart_id == self.row.id else None

    async def cambiar_estado(self, cart_id, status):
        if cart_id == self.row.id:
            self.row.status = status
        return self.row


class FakeCartItems:
    async def listar_por_cart(self, cart_id):
        return [
            SimpleNamespace(
                product_id="prod-1",
                sku="SKU-1",
                name="Polo",
                quantity=2,
                unit_price_ref=50.0,
                image_url=None,
            )
        ]


class FakeOrders:
    def __init__(self):
        self.rows: dict[uuid.UUID, SimpleNamespace] = {}
        self.updated: list[tuple[str, str]] = []

    async def guardar(self, row):
        self.rows[row.checkout_id] = SimpleNamespace(
            order_id=row.order_id, local_status=row.local_status
        )
        return self.rows[row.checkout_id]

    async def obtener_por_checkout_id(self, checkout_id):
        return self.rows.get(checkout_id)

    async def actualizar_estado(self, order_id, local_status):
        self.updated.append((order_id, local_status))
        for row in self.rows.values():
            if row.order_id == order_id:
                row.local_status = local_status


class FakeOutbox:
    def __init__(self):
        self.tasks: list[object] = []

    async def guardar(self, task):
        self.tasks.append(task)
        return task


def build(*, sales, deadline=30.0, gateway=None):
    checkouts = InMemoryCheckouts()
    carts = FakeCarts()
    orders = FakeOrders()
    outbox = FakeOutbox()
    uow = FakeUow()
    payment_attempts = MagicMock()
    payment_attempts.obtener_por_idempotency_key = AsyncMock(return_value=None)
    use_case = ManageCheckoutUseCase(
        checkouts=checkouts,
        payment_attempts=payment_attempts,
        payment_gateway=gateway or MagicMock(process_payment=AsyncMock()),
        uow=uow,
        carts=carts,
        cart_items=FakeCartItems(),
        orders=orders,
        sales_client=sales,
        sales_deadline_seconds=deadline,
        outbox=outbox,
        dispatch=FakeDispatch(),
    )
    return use_case, checkouts, carts, orders, outbox, uow


def payload(key: str) -> CreateCheckoutInput:
    return CreateCheckoutInput(
        customer_id=CUSTOMER,
        idempotency_key=key,
        cart_id=CART,
        direccion=ADDRESS,
        contacto=CONTACT,
    )


async def _timed_out_checkout(key="key-timeout"):
    sales = FakeSalesPort(delay=0.05)
    use_case, checkouts, carts, orders, outbox, uow = build(sales=sales, deadline=0.005)
    with pytest.raises(CheckoutError) as exc_info:
        await use_case.create_checkout(payload(key))
    return sales, use_case, checkouts, carts, orders, outbox, uow, exc_info.value


@pytest.mark.asyncio
async def test_sales_timeout_persists_checkout_and_returns_recoverable_ids():
    sales, _, checkouts, carts, _, _, uow, error = await _timed_out_checkout()

    assert error.code == "MODULO_NO_DISPONIBLE"
    assert error.extra["motivo"] == "TIMEOUT_VENTAS"
    assert error.extra["reintentable"] is True
    assert error.extra["orderIdProvisional"] is True

    checkout = checkouts.by_key["key-timeout"]
    assert error.extra["checkoutId"] == str(checkout.id)
    assert error.extra["orderId"] == checkout.summary["orderId"]
    assert checkout.status == CheckoutStatus.PENDING_PAYMENT.value
    assert checkout.summary[SALES_PENDING_KEY] is True
    assert SALES_SNAPSHOT_KEY in checkout.summary
    assert carts.row.status == "IN_CHECKOUT"
    # One commit before the external call: the row survives the timeout.
    assert uow.commits == 1
    # The fake Ventas created the order before its response timed out.
    assert sales.orders


@pytest.mark.asyncio
async def test_same_key_retry_reconciles_the_order_without_duplicates():
    sales, use_case, checkouts, _, orders, _, uow, error = await _timed_out_checkout()
    provisional_id = error.extra["orderId"]
    sales.delay = 0

    checkout = await use_case.create_checkout(payload("key-timeout"))

    real_id = sales.orders["key-timeout"]
    assert real_id != provisional_id
    assert checkout.summary["orderId"] == real_id
    assert SALES_PENDING_KEY not in checkout.summary
    assert SALES_SNAPSHOT_KEY not in checkout.summary
    assert sales.calls == ["key-timeout", "key-timeout"]
    assert len(sales.orders) == 1
    assert len(checkouts.by_key) == 1
    assert checkouts.by_key["key-timeout"].id == checkout.id
    assert orders.rows[checkout.id].order_id == real_id
    assert uow.commits == 2


@pytest.mark.asyncio
async def test_new_key_retry_reuses_the_pending_checkout_without_duplicates():
    sales, use_case, checkouts, _, _, _, _, _ = await _timed_out_checkout("key-timeout")
    sales.delay = 0

    checkout = await use_case.create_checkout(payload("key-new-attempt"))

    assert checkout.id == checkouts.by_key["key-timeout"].id
    assert checkout.idempotency_key == "key-timeout"
    assert len(checkouts.by_key) == 1
    assert checkout.summary["orderId"] == sales.orders["key-timeout"]
    assert sales.orders.keys() == {"key-timeout"}


@pytest.mark.asyncio
async def test_replaced_pending_checkout_cancels_its_sales_order():
    sales, use_case, checkouts, _, orders, outbox, _, _ = await _timed_out_checkout("key-old")
    old = checkouts.by_key["key-old"]
    sales.delay = 0

    new = await use_case.create_checkout(
        CreateCheckoutInput(
            customer_id=CUSTOMER,
            idempotency_key="key-changed-address",
            cart_id=CART,
            direccion={**ADDRESS, "distrito": "Surco"},
            contacto=CONTACT,
        )
    )

    old_real_id = sales.orders["key-old"]
    assert old.status == CheckoutStatus.EXPIRED.value
    assert orders.updated == [(old_real_id, "CANCELLED")]
    assert outbox.tasks[-1].payload["order_id"] == old_real_id
    assert new.id != old.id
    assert new.summary["orderId"] == sales.orders["key-changed-address"]


@pytest.mark.asyncio
async def test_happy_path_keeps_one_sales_call_and_real_order_id():
    sales = FakeSalesPort()
    use_case, checkouts, carts, orders, _, uow = build(sales=sales, deadline=1.0)

    checkout = await use_case.create_checkout(payload("key-happy"))

    assert sales.calls == ["key-happy"]
    assert checkout.summary["orderId"] == sales.orders["key-happy"]
    assert SALES_PENDING_KEY not in checkout.summary
    assert SALES_SNAPSHOT_KEY not in checkout.summary
    assert checkouts.by_key["key-happy"].id == checkout.id
    assert carts.row.status == "IN_CHECKOUT"
    assert orders.rows[checkout.id].order_id == sales.orders["key-happy"]
    assert uow.commits == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ventas_status,code",
    [(409, "STOCK_INSUFICIENTE"), (400, "DATO_INVALIDO")],
)
async def test_definitive_sales_rejection_retires_checkout_and_frees_cart(ventas_status, code):
    sales = FakeSalesPort(error=ModuleRequestError("sales", ventas_status, "RECHAZADO"))
    use_case, checkouts, carts, _, _, _ = build(sales=sales, deadline=1.0)

    with pytest.raises(CheckoutError) as exc_info:
        await use_case.create_checkout(payload("key-definitive"))

    assert exc_info.value.code == code
    checkout = checkouts.by_key["key-definitive"]
    assert checkout.status == CheckoutStatus.EXPIRED.value
    assert SALES_PENDING_KEY not in checkout.summary
    assert SALES_SNAPSHOT_KEY not in checkout.summary
    assert carts.row.status == "ACTIVE"
    assert await checkouts.obtener_vivo_por_cart(CART) is None

    # The retry with a new key is not blocked by a live checkout.
    sales.error = None
    retry = await use_case.create_checkout(payload("key-after-definitive"))
    assert retry.id != checkout.id


@pytest.mark.asyncio
async def test_payment_is_blocked_while_the_sales_order_is_provisional():
    sales, use_case, checkouts, _, _, _, _, _ = await _timed_out_checkout()
    gateway = MagicMock(process_payment=AsyncMock())
    use_case.payment_gateway = gateway
    checkout = checkouts.by_key["key-timeout"]

    with pytest.raises(CheckoutError) as exc_info:
        await use_case.process_payment(
            ProcessPaymentInput(
                checkout_id=checkout.id,
                customer_id=CUSTOMER,
                idempotency_key="pay-pending",
                numero_tarjeta="4111 1111 1111 1111",
                titular="Juan Pérez",
                vencimiento="12/28",
                cvv="123",
            )
        )

    assert exc_info.value.code == "MODULO_NO_DISPONIBLE"
    assert exc_info.value.extra["motivo"] == "PEDIDO_EN_PROCESO"
    assert exc_info.value.extra["reintentable"] is True
    gateway.process_payment.assert_not_awaited()
    assert sales.orders


@pytest.mark.asyncio
async def test_expiry_reconciles_the_pending_order_and_cancels_the_real_id():
    sales, use_case, checkouts, _, orders, outbox, _, _ = await _timed_out_checkout("key-expire")
    checkout = checkouts.by_key["key-expire"]
    checkout.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    sales.delay = 0

    assert await use_case.expire_checkout(checkout.id) is True

    real_id = sales.orders["key-expire"]
    assert checkout.status == CheckoutStatus.EXPIRED.value
    assert sales.calls == ["key-expire", "key-expire"]
    assert len(sales.orders) == 1
    assert orders.updated == [(real_id, "CANCELLED")]
    assert outbox.tasks[-1].type == "REQUEST_CANCELLATION"
    assert outbox.tasks[-1].payload["order_id"] == real_id
