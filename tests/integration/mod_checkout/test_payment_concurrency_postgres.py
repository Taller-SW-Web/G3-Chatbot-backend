"""Payment attempt guarantees enforced by PostgreSQL (BE-AUD-03)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.modules.mod_checkout.adapters.checkout_postgres_adapter import CheckoutPostgresAdapter
from src.modules.mod_checkout.adapters.payment_attempt_postgres_adapter import (
    PaymentAttemptPostgresAdapter,
)
from src.modules.mod_checkout.domain.payment_attempt import NewPaymentAttempt, PaymentConflictError
from src.shared.infrastructure.db.models import Cart, Checkout

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _checkout(session) -> Checkout:
    customer = uuid.uuid4()
    cart = Cart(customer_id=customer)
    session.add(cart)
    await session.flush()
    row = Checkout(
        cart_id=cart.id, customer_id=customer, summary={}, total=10,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15), idempotency_key=uuid.uuid4().hex,
    )
    session.add(row)
    await session.flush()
    return row


def _attempt(checkout_id, result="APPROVED") -> NewPaymentAttempt:
    return NewPaymentAttempt(
        checkout_id=checkout_id, idempotency_key=uuid.uuid4().hex, result=result,
        brand="VISA", last4="1111", amount=10,
        reason=None if result == "APPROVED" else "DECLINED_BY_ISSUER",
    )


async def test_second_approved_attempt_for_a_checkout_is_a_conflict(postgres_session):
    checkout = await _checkout(postgres_session)
    attempts = PaymentAttemptPostgresAdapter(postgres_session)
    await attempts.guardar(_attempt(checkout.id))
    with pytest.raises(PaymentConflictError):
        await attempts.guardar(_attempt(checkout.id))


async def test_rejected_attempts_are_not_limited_by_the_approved_index(postgres_session):
    checkout = await _checkout(postgres_session)
    attempts = PaymentAttemptPostgresAdapter(postgres_session)
    for _ in range(3):
        await attempts.guardar(_attempt(checkout.id, "REJECTED"))
    await attempts.guardar(_attempt(checkout.id))


async def test_attempt_counter_is_atomic_and_capped_at_three(postgres_session):
    checkout = await _checkout(postgres_session)
    adapter = CheckoutPostgresAdapter(postgres_session)
    assert [(await adapter.incrementar_intentos(checkout.id)).payment_attempts for _ in range(3)] == [1, 2, 3]
    assert await adapter.incrementar_intentos(checkout.id) is None


async def test_for_update_lookup_returns_the_row(postgres_session):
    checkout = await _checkout(postgres_session)
    adapter = CheckoutPostgresAdapter(postgres_session)
    assert (await adapter.obtener_por_id(checkout.id, for_update=True)).id == checkout.id
