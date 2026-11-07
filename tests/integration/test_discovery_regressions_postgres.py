"""Review regressions against an explicitly configured disposable test database."""
import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.adapters.inbound.http.catalog_router import create_catalog_router
from src.adapters.inbound.http.chatbot_router import create_chatbot_router
from src.adapters.inbound.http.dependencies.caller_identity import get_caller_identity
from src.adapters.inbound.http.problem import register_problem_handlers
from src.adapters.outbound.persistence.conversation_postgres_adapter import ConversationPostgresAdapter
from src.adapters.outbound.persistence.message_postgres_adapter import MessagePostgresAdapter
from src.adapters.outbound.persistence.models.conversation import Conversation
from src.adapters.outbound.persistence.models.message import Message
from src.adapters.outbound.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.adapters.outbound.tools.registry_tool_adapter import RegistryToolAdapter
from src.application.tools.action_dispatcher import ActionDispatcher
from src.application.tools.catalog_handlers import build_catalog_handlers
from src.application.tools.catalog_tools import EmptyArguments, RecommendationArguments, build_tool_registry
from src.application.tools.tool_registry import ExecutionContext
from src.application.use_cases.dispatch_direct_action_use_case import DispatchDirectActionUseCase
from src.application.use_cases.interpret_and_respond_use_case import InterpretAndRespondUseCase
from src.application.use_cases.manage_catalog_use_case import ManageCatalogUseCase
from src.application.use_cases.manage_conversation_use_case import ManageConversationUseCase
from src.domain.value_objects.caller_identity import CallerIdentity
from tests.fakes.fake_llm_provider import FakeLLMProvider
from tests.unit.test_discovery import PROMO, service
from tests.unit.test_interpret_and_respond_use_case import reply, tool_turn

OWNER = CallerIdentity(anonymous_sid="astra-disposable-owner")


@pytest_asyncio.fixture
async def database():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    schema = "astra_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as con:
        await con.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": schema, "statement_timeout": "10000"}},
        json_serializer=lambda v: json.dumps(v, default=str),
    )
    meta = MetaData()
    Conversation.__table__.to_metadata(meta)
    Message.__table__.to_metadata(meta)
    async with engine.begin() as con:
        await con.run_sync(meta.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as s:
        cid = (await ConversationPostgresAdapter(s).create(OWNER)).id
        await s.commit()
    try:
        yield (sessions, cid)
    finally:
        await engine.dispose()
        async with admin.begin() as con:
            await con.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


def catalog(s):
    seed, _ = service()
    return ManageCatalogUseCase(seed._catalog, seed._inventory, ConversationPostgresAdapter(s), SqlAlchemyUnitOfWork(s))


def context(cid):
    return ExecutionContext(conversacion_id=str(cid), chat_sid=OWNER.anonymous_sid)


@pytest.mark.asyncio
async def test_concurrent_recommendation_does_not_erase_confirmed_silence(database):
    sessions, cid = database
    entered = asyncio.Event()
    release = asyncio.Event()
    async with sessions() as a, sessions() as b:
        first = catalog(a)
        second = catalog(b)
        original = first._catalog.search_products

        async def delayed(**kw):
            entered.set()
            await release.wait()
            return await original(**kw)

        first._catalog.search_products = delayed
        pending = asyncio.create_task(first.recommend(RecommendationArguments(actividad="running"), context(cid)))
        await asyncio.wait_for(entered.wait(), 5)
        await second.silence(EmptyArguments(), context(cid))
        release.set()
        await asyncio.wait_for(pending, 10)
    async with sessions() as s:
        state = await ConversationPostgresAdapter(s).get_context(cid)
        assert state.get("crossSellSilenciado") is True, f"Confirmed silence lost: {state}"


@pytest.mark.asyncio
async def test_concurrent_cross_sell_same_source_emits_once(database):
    sessions, cid = database
    async with sessions() as a, sessions() as b:
        results = await asyncio.wait_for(
            asyncio.gather(catalog(a).cross_sell("0", context(cid)), catalog(b).cross_sell("0", context(cid))), 10
        )
        assert sum((bool(r) for r in results)) == 1


@pytest.mark.asyncio
async def test_local_http_actions_stream_and_history_real_db_export(database):
    sessions, cid = database

    async def history_service():
        async with sessions() as s:
            yield ManageConversationUseCase(
                ConversationPostgresAdapter(s), MessagePostgresAdapter(s), SqlAlchemyUnitOfWork(s)
            )

    async def catalog_service():
        async with sessions() as s:
            uc = catalog(s)
            uc._catalog.list_promotions = AsyncMock(return_value=[PROMO])
            yield uc

    @asynccontextmanager
    async def actions():
        async with sessions() as s:
            uc = catalog(s)
            uc._catalog.list_promotions = AsyncMock(return_value=[PROMO])
            yield DispatchDirectActionUseCase(
                ConversationPostgresAdapter(s),
                MessagePostgresAdapter(s),
                SqlAlchemyUnitOfWork(s),
                ActionDispatcher(build_tool_registry(build_catalog_handlers(uc))),
            )

    @asynccontextmanager
    async def turns():
        async with sessions() as s:
            uc = catalog(s)
            llm = FakeLLMProvider(
                [tool_turn("need", "recomendar_productos", {"actividad": "running"}), reply("Opciones del servidor.")]
            )
            yield InterpretAndRespondUseCase(
                ConversationPostgresAdapter(s),
                MessagePostgresAdapter(s),
                SqlAlchemyUnitOfWork(s),
                llm,
                RegistryToolAdapter(build_tool_registry(build_catalog_handlers(uc))),
                assistant_name="Botleta",
                llm_timeout_seconds=5,
            )

    app = FastAPI()
    register_problem_handlers(app)
    app.dependency_overrides[get_caller_identity] = lambda: OWNER
    app.include_router(
        create_chatbot_router(history_service, cookie_secure=False, action_scope=actions, turn_scope=turns),
        prefix="/api/v1",
    )
    app.include_router(create_catalog_router(catalog_service), prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://astra-local") as client:
        path = f"/api/v1/chat/conversaciones/{cid}/mensajes"
        offered = await client.get("/api/v1/catalogo/productos?soloOfertas=true")
        assert offered.status_code == 200
        offers = await client.post(path, json={"accion": {"tipo": "VER_OFERTAS", "payload": {}}})
        assert offers.json()["code"] == "OK"
        question = await client.post(
            path, json={"accion": {"tipo": "RECOMENDAR_PRODUCTOS", "payload": {"actividad": "futbol"}}}
        )
        assert question.json()["code"] == "OK"
        chosen = question.json()["bloques"][0]["acciones"][0]
        clarification = await client.post(path, json={"accion": {"tipo": chosen["id"], "payload": chosen["payload"]}})
        assert clarification.json()["code"] == "OK"
        detail = await client.post(
            path, json={"accion": {"tipo": "VER_DETALLE_PRODUCTO", "payload": {"product_id": "0"}}}
        )
        assert detail.json()["code"] == "OK"
        assert any((b["datos"].get("variant") == "cross_sell" for b in detail.json()["bloques"]))
        silence = await client.post(path, json={"accion": {"tipo": "SILENCIAR_COMPLEMENTOS", "payload": {}}})
        assert silence.json()["code"] == "OK"
        detail_again = await client.post(
            path, json={"accion": {"tipo": "VER_DETALLE_PRODUCTO", "payload": {"product_id": "4"}}}
        )
        assert not any((b["datos"].get("variant") == "cross_sell" for b in detail_again.json()["bloques"]))
        stream = await client.post(path, json={"texto": "Recomienda running"}, headers={"Accept": "text/event-stream"})
        assert stream.status_code == 200 and "event: fin" in stream.text
        history = await client.get(path)
        assert history.status_code == 200
        app.dependency_overrides[get_caller_identity] = lambda: CallerIdentity(anonymous_sid="stranger")
        assert (await client.get(path)).status_code == 404
