"""reset_checkout against PostgreSQL (needs TEST_DATABASE_URL, skipped otherwise).

Regression: clearing the cart raised a raw IntegrityError because the update wrote JSON
`null` (not SQL NULL) into the snapshot columns, violating the jsonb_typeof = 'object' CHECKs.
"""

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.adapters.outbound.persistence.cart_postgres_adapter import CartPostgresAdapter
from src.adapters.outbound.persistence.models.cart import Cart
from src.adapters.outbound.persistence.models.conversation import Conversation


@pytest_asyncio.fixture
async def sessions():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    schema = "astra_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as con:
        await con.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": f"{schema},public"}})
    meta = MetaData()
    Conversation.__table__.to_metadata(meta)
    Cart.__table__.to_metadata(meta)
    async with engine.begin() as con:
        await con.run_sync(meta.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as con:
            await con.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_reset_checkout_stores_sql_null_in_snapshots(sessions):
    async with sessions() as s:
        cart = Cart(
            customer_id=uuid.uuid4(),
            coupon_code="X",
            shipping_snapshot={"costo": 5},
            totals_snapshot={"total": "10"},
        )
        s.add(cart)
        await s.flush()
        cart_id = cart.id

        await CartPostgresAdapter(s).reset_checkout(cart_id)
        await s.commit()

        row = (await s.execute(
            text("select coupon_code, shipping_snapshot is null, totals_snapshot is null "
                 "from cart where id = :i"), {"i": cart_id},
        )).one()
    assert tuple(row) == (None, True, True)
