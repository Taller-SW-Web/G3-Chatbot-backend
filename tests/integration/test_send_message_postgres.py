"""A full SSE turn through the router against PostgreSQL 18 with a fake LLM and tools (needs TEST_DATABASE_URL)."""
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from src.adapters.inbound.http.chatbot_router import create_chatbot_router
from src.adapters.inbound.http.dependencies.caller_identity import hash_chat_sid
from src.adapters.inbound.http.problem import register_problem_handlers
from src.adapters.outbound.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)
from src.adapters.outbound.persistence.message_postgres_adapter import MessagePostgresAdapter
from src.adapters.outbound.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.application.use_cases.interpret_and_respond_use_case import InterpretAndRespondUseCase
from src.domain.entities.message import MessageRole
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.outbound.llm_provider_port import Completed, LLMUnavailableError, TextDelta, ToolCall, ToolCallRequested
from src.ports.outbound.tool_executor_port import ToolResult
from tests.fakes.fake_llm_provider import FakeLLMProvider
from tests.fakes.fake_tool_executor import FakeToolExecutor

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SID = "browser-cookie"
VISITOR = CallerIdentity(anonymous_sid=hash_chat_sid(SID))


def make_client(session, llm, tools):
    @asynccontextmanager
    async def scope():
        yield InterpretAndRespondUseCase(
            ConversationPostgresAdapter(session), MessagePostgresAdapter(session),
            SqlAlchemyUnitOfWork(session), llm, tools, assistant_name="Botleta", llm_timeout_seconds=5,
        )

    app = FastAPI()
    register_problem_handlers(app)
    app.include_router(
        create_chatbot_router(lambda: None, cookie_secure=False, turn_scope=scope), prefix="/api/v1"
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", cookies={"chat_sid": SID}
    )


def frames(text):
    out = []
    for chunk in filter(None, text.split("\n\n")):
        event, data = chunk.split("\n")
        out.append((event[7:], json.loads(data[6:])))
    return out


async def test_a_whole_turn_over_sse_is_persisted_and_ends_with_fin(postgres_session):
    """SPEC-05 Req. 3 and 4: wire frames of a tool turn, and the rows they correspond to."""
    conversations, messages = ConversationPostgresAdapter(postgres_session), MessagePostgresAdapter(postgres_session)
    cid = (await conversations.create(VISITOR)).id
    block = {"id": "b1", "tipo": "CARRUSEL_PRODUCTOS", "productos": []}
    llm = FakeLLMProvider([
        [ToolCallRequested(ToolCall("c1", "buscar_productos", {"marca": "Nike"})), Completed("tool_calls")],
        [TextDelta("Encontré "), TextDelta("dos opciones"), Completed("stop")],
    ])
    tools = FakeToolExecutor({"buscar_productos": ToolResult("OK", {"total": 2}, block=block)})

    async with make_client(postgres_session, llm, tools) as client:
        res = await client.post(f"/api/v1/chat/conversaciones/{cid}/mensajes", json={"texto": "zapatillas Nike"})

    assert res.status_code == 200 and res.headers["content-type"].startswith("text/event-stream")
    events = frames(res.text)
    assert [name for name, _ in events] == ["aceptado", "bloque", "token", "token", "fin"]
    assert events[1][1] == {"bloque": block}
    stored = await messages.list_recent(cid, limit=50)
    assert [m.role for m in stored] == [MessageRole.CUSTOMER, MessageRole.TOOL, MessageRole.ASSISTANT]
    assert events[0][1]["mensajeId"] == str(stored[0].id) and events[-1][1]["mensajeId"] == str(stored[-1].id)
    assert stored[-1].text == "Encontré dos opciones"
    assert (await conversations.find_owned(cid, VISITOR)).title == "zapatillas Nike"


async def test_a_degraded_turn_streams_the_spec_shaped_quick_actions(postgres_session):
    conversations = ConversationPostgresAdapter(postgres_session)
    cid = (await conversations.create(VISITOR)).id
    llm = FakeLLMProvider([LLMUnavailableError("down")])

    async with make_client(postgres_session, llm, FakeToolExecutor()) as client:
        res = await client.post(f"/api/v1/chat/conversaciones/{cid}/mensajes", json={"texto": "hola"})

    events = frames(res.text)
    assert [name for name, _ in events] == ["aceptado", "degradado", "bloque", "fin"]
    block = events[2][1]["bloque"]
    assert block["tipo"] == "ACCIONES_RAPIDAS" and len(block["acciones"]) == 6
    assert set(block["acciones"][0]) == {"id", "etiqueta"}


async def test_a_conversation_of_another_visitor_is_a_404_and_nothing_is_stored(postgres_session):
    conversations, messages = ConversationPostgresAdapter(postgres_session), MessagePostgresAdapter(postgres_session)
    cid = (await conversations.create(CallerIdentity(anonymous_sid=hash_chat_sid("someone-else")))).id
    llm = FakeLLMProvider([])

    async with make_client(postgres_session, llm, FakeToolExecutor()) as client:
        res = await client.post(f"/api/v1/chat/conversaciones/{cid}/mensajes", json={"texto": "hola"})

    assert res.status_code == 404 and res.json()["code"] == "RECURSO_NO_ENCONTRADO"
    assert await messages.list_recent(cid, limit=50) == [] and llm.calls == []
