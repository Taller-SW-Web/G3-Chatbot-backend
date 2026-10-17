"""Conversation working memory against PostgreSQL 18 (needs TEST_DATABASE_URL).

Regression: the catalog use case used to assign `context` on the frozen entity returned by
`find_owned`, so every search inside a conversation failed and carousel ordinals never resolved.
"""
import uuid

import pytest

from src.adapters.outbound.fakes.fake_products import FakeCatalogPort, FakeInventoryPort
from src.adapters.outbound.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)
from src.adapters.outbound.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.application.tools.catalog_tools import ProductArguments, SearchArguments
from src.application.tools.tool_registry import ExecutionContext
from src.application.use_cases.manage_catalog_use_case import CatalogError, ManageCatalogUseCase
from src.domain.value_objects.caller_identity import CallerIdentity

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SID_HASH = "hashed-browser-sid"
OWNER = CallerIdentity(anonymous_sid=SID_HASH)
STRANGER = CallerIdentity(anonymous_sid="someone-else")


def context_for(conversation_id, sid=SID_HASH):
    return ExecutionContext(conversacion_id=str(conversation_id), cliente_id=None, chat_sid=sid)


def make_catalog(session):
    return ManageCatalogUseCase(
        FakeCatalogPort(), FakeInventoryPort({}),
        ConversationPostgresAdapter(session), SqlAlchemyUnitOfWork(session),
    )


async def test_context_round_trips_and_defaults_to_empty(postgres_session):
    adapter = ConversationPostgresAdapter(postgres_session)
    cid = (await adapter.create(OWNER)).id

    assert await adapter.get_context(cid) == {}
    assert await adapter.get_context(uuid.uuid4()) == {}

    await adapter.save_context(cid, {"ultimoCarrusel": ["a", "b"]})
    await postgres_session.commit()

    assert await adapter.get_context(cid) == {"ultimoCarrusel": ["a", "b"]}


async def test_search_persists_the_carousel_and_a_later_read_resolves_the_ordinal(postgres_session):
    conversations = ConversationPostgresAdapter(postgres_session)
    cid = (await conversations.create(OWNER)).id
    catalog = make_catalog(postgres_session)

    result = await catalog.search(SearchArguments(q="Nike"), context_for(cid))

    assert result["bloques"][0]["tipo"] == "CARRUSEL_PRODUCTOS"
    memory = await conversations.get_context(cid)
    assert memory["ultimoCarrusel"]
    assert memory["filtrosVigentes"]["q"] == "Nike"

    detail = await make_catalog(postgres_session).detail(ProductArguments(productoRef=1), context_for(cid))
    assert detail["bloques"][0]["tipo"] == "DETALLE_PRODUCTO"
    assert detail["bloques"][0]["datos"]["producto"]["product_id"] == memory["ultimoCarrusel"][0]


async def test_a_stranger_cannot_read_or_write_the_memory(postgres_session):
    conversations = ConversationPostgresAdapter(postgres_session)
    cid = (await conversations.create(OWNER)).id

    with pytest.raises(CatalogError) as raised:
        await make_catalog(postgres_session).search(
            SearchArguments(q="Nike"), context_for(cid, sid=STRANGER.anonymous_sid)
        )

    assert raised.value.code == "RECURSO_NO_ENCONTRADO"
    assert await conversations.get_context(cid) == {}
