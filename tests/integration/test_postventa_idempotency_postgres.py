"""Postventa local references are really committed (needs TEST_DATABASE_URL, PostgreSQL 18+).

Unlike the transactional `postgres_session` fixture, this module uses an isolated schema
and independent sessions with real commits, so a retry that opens a new session (as each
HTTP request does) can only find the reference if it was committed.
"""
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import MetaData, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.adapters.outbound.fakes.fake_postventa import FakePostventaPort
from src.adapters.outbound.fakes.fake_ventas import FakeSalesPort
from src.adapters.outbound.persistence.claim_ref_postgres_adapter import ClaimRefPostgresAdapter
from src.adapters.outbound.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)
from src.adapters.outbound.persistence.evidence_postgres_adapter import EvidencePostgresAdapter
from src.adapters.outbound.persistence.models import ClaimRef, Conversation, Evidence, ReturnRef
from src.adapters.outbound.persistence.return_ref_postgres_adapter import ReturnRefPostgresAdapter
from src.adapters.outbound.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.application.dto.postventa_dto import PostventaError
from src.application.use_cases.postventa_service import PostventaService

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class CountingPostventa(FakePostventaPort):
    def __init__(self) -> None:
        super().__init__()
        self.create_return_calls = 0
        self.create_claim_calls = 0
        self.upload_calls = 0

    async def create_return(self, payload, **kwargs):
        self.create_return_calls += 1
        return await super().create_return(payload, **kwargs)

    async def create_claim(self, payload, **kwargs):
        self.create_claim_calls += 1
        return await super().create_claim(payload, **kwargs)

    async def upload_evidence(self, **kwargs):
        self.upload_calls += 1
        return await super().upload_evidence(**kwargs)


@pytest_asyncio.fixture
async def sessions():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Falta TEST_DATABASE_URL: integración PostgreSQL 18 no ejecutada")
    schema = f"postventa_test_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    metadata = MetaData()
    for model in (Conversation, ReturnRef, ClaimRef, Evidence):
        model.__table__.to_metadata(metadata)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


def build_service(session: AsyncSession, postventa: CountingPostventa) -> PostventaService:
    return PostventaService(
        postventa=postventa,
        sales=FakeSalesPort(),
        claim_refs=ClaimRefPostgresAdapter(session),
        return_refs=ReturnRefPostgresAdapter(session),
        evidence=EvidencePostgresAdapter(session),
        conversations=ConversationPostgresAdapter(session),
        uow=SqlAlchemyUnitOfWork(session),
    )


PostventaPort_ = CountingPostventa | None

CUSTOMER = uuid.uuid4()
HOLDER = f"fake-access-{CUSTOMER}"
RETURN_PAYLOAD = {
    "pedidoId": "PED-1",
    "tipo": "CAMBIO",
    "motivo": "OTRO",
    "items": [{"productoId": "P-1", "cantidad": 1}],
    "evidencias": [],
}


async def test_return_retry_with_same_key_reuses_committed_ref_and_skips_g5(sessions):
    postventa = CountingPostventa()
    # Seed the fake G5 with the customer's order so get_customer_return can serve it.
    async with sessions() as first:
        created = await build_service(first, postventa).create_return(
            customer_id=CUSTOMER,
            holder_token=HOLDER,
            payload=dict(RETURN_PAYLOAD),
            idempotency_key="return-retry-1",
        )
    assert postventa.create_return_calls == 1

    # A brand-new session (a new HTTP request) only sees what was committed.
    async with sessions() as second:
        retried = await build_service(second, postventa).create_return(
            customer_id=CUSTOMER,
            holder_token=HOLDER,
            payload=dict(RETURN_PAYLOAD),
            idempotency_key="return-retry-1",
        )
        rows = (await second.execute(select(ReturnRef))).scalars().all()

    assert postventa.create_return_calls == 1
    assert retried["devolucionId"] == created["devolucionId"]
    assert [r.idempotency_key for r in rows] == ["return-retry-1"]
    assert rows[0].requested_type == "EXCHANGE"


async def test_claim_retry_with_same_key_reuses_committed_ref_and_skips_g5(sessions):
    postventa = CountingPostventa()
    sales = FakeSalesPort()
    order = await sales.create_order(
        {
            "canal": "CHATBOT",
            "contacto": {"clienteId": str(CUSTOMER), "nombreCompleto": "Ana"},
            "items": [{"productoId": "P-1", "sku": "SKU-1", "cantidad": 1, "precioUnitario": 10}],
            "envio": {"distrito": "Miraflores", "costo": 0},
            "pago": {"total": 10},
        },
        idempotency_key="order-claim-1",
    )
    payload = {
        "pedidoId": order["pedidoId"],
        "tipo": "RECLAMO",
        "motivo": "PRODUCTO_DEFECTUOSO",
        "detalle": "La suela se despegó al primer uso.",
        "consumidor": {
            "nombreCompleto": "Ana",
            "documento": "72458912",
            "email": "ana@example.com",
            "telefono": "+51999888777",
        },
    }

    async def attempt():
        async with sessions() as session:
            service = build_service(session, postventa)
            service._sales = sales
            return await service.create_claim(
                customer_id=CUSTOMER,
                holder_token=HOLDER,
                payload=dict(payload),
                idempotency_key="claim-retry-1",
            )

    first, second = await attempt(), await attempt()

    assert postventa.create_claim_calls == 1
    assert first["codigoSeguimiento"] == second["codigoSeguimiento"]


async def test_evidence_ownership_is_checked_against_real_conversations(sessions):
    postventa = CountingPostventa()
    async with sessions() as session:
        mine = await ConversationPostgresAdapter(session).create(
            _caller(CUSTOMER)
        )
        foreign = await ConversationPostgresAdapter(session).create(_caller(uuid.uuid4()))
        await session.commit()

    async def upload(conversation_id):
        async with sessions() as session:
            return await build_service(session, postventa).upload_evidence(
                holder_token=HOLDER,
                file_name="foto.png",
                content_type="image/png",
                content=b"png",
                conversation_id=conversation_id,
                customer_id=CUSTOMER,
            )

    with pytest.raises(PostventaError) as exc_info:
        await upload(foreign.id)
    assert exc_info.value.code == "RECURSO_NO_ENCONTRADO"
    with pytest.raises(PostventaError):
        await upload(uuid.uuid4())
    assert postventa.upload_calls == 0

    await upload(mine.id)
    assert postventa.upload_calls == 1
    async with sessions() as session:
        rows = (await session.execute(select(Evidence))).scalars().all()
    assert [r.conversation_id for r in rows] == [mine.id]


async def test_duplicate_key_does_not_discard_other_pending_work(sessions):
    """A lost idempotency race must not roll back the rest of the caller's transaction."""
    async with sessions() as session:
        conversation = await ConversationPostgresAdapter(session).create(_caller(CUSTOMER))
        adapter = ReturnRefPostgresAdapter(session)
        await adapter.guardar(_return_ref("DEV-A", "key-dup"))
        await session.commit()

    async with sessions() as session:
        await EvidencePostgresAdapter(session).guardar(
            Evidence(
                conversation_id=conversation.id,
                type="IMAGE",
                url="https://g5.example/e.png",
                original_file_name="e.png",
                size_bytes=10,
            )
        )
        existing = await ReturnRefPostgresAdapter(session).guardar(_return_ref("DEV-B", "key-dup"))
        assert existing.return_id == "DEV-A"
        await session.commit()

    async with sessions() as session:
        assert len((await session.execute(select(Evidence))).scalars().all()) == 1
        refs = (await session.execute(select(ReturnRef))).scalars().all()
        assert [r.return_id for r in refs] == ["DEV-A"]


def _caller(customer_id):
    from src.domain.value_objects.caller_identity import CallerIdentity

    return CallerIdentity(customer_id=customer_id)


def _return_ref(return_id: str, key: str) -> ReturnRef:
    return ReturnRef(
        return_id=return_id,
        code=return_id,
        order_id="PED-1",
        customer_id=CUSTOMER,
        requested_type="CAMBIO",
        idempotency_key=key,
    )
