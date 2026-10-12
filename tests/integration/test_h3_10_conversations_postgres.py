"""H3-10: conversation queries against PostgreSQL 18 (needs TEST_DATABASE_URL, skipped otherwise)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.outbound.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)
from src.adapters.outbound.persistence.message_postgres_adapter import MessagePostgresAdapter
from src.adapters.outbound.persistence.models import Conversation, Message
from src.domain.entities.message import MessageRole
from src.domain.value_objects.caller_identity import CallerIdentity

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def titled(adapter, owner, title, minute):
    row = await adapter.guardar(Conversation(
        customer_id=owner.customer_id, anonymous_sid=owner.anonymous_sid, title=title,
        last_message_at=T0 + timedelta(minutes=minute),
    ))
    return row.id


async def test_nueva_conversacion_queda_vacia_y_fuera_del_listado(postgres_session):
    """SPEC-05 Req. 1 · Scenario: Nueva conversación desde la barra lateral."""
    adapter = ConversationPostgresAdapter(postgres_session)
    owner = CallerIdentity(customer_id=uuid.uuid4())
    created = await adapter.create(owner)
    assert created.title is None and created.last_message_at is not None
    assert await adapter.list_summaries(owner, limit=20, offset=0) == []
    assert (await adapter.find_owned(created.id, owner)).id == created.id


async def test_listado_ordena_pagina_y_aisla_por_dueno_con_vista_previa(postgres_session):
    """SPEC-05 Req. 1 · Scenario: Listado de conversaciones recientes."""
    session = postgres_session
    adapter, messages = ConversationPostgresAdapter(session), MessagePostgresAdapter(session)
    ana = CallerIdentity(customer_id=uuid.uuid4())
    visitor = CallerIdentity(anonymous_sid="hash-visitor")
    ids = [await titled(adapter, ana, f"chat {i}", i) for i in range(3)]
    await titled(adapter, visitor, "anon", 5)
    await adapter.create(ana)  # empty one: no title, must not be listed
    await messages.guardar(Message(conversation_id=ids[2], role="CUSTOMER", content="primero", created_at=T0))
    await messages.guardar(Message(conversation_id=ids[2], role="ASSISTANT", content="respuesta", created_at=T0 + timedelta(minutes=1)))
    await messages.guardar(Message(conversation_id=ids[2], role="TOOL", tool_name="t", content="ignorado", created_at=T0 + timedelta(minutes=2)))

    page1 = await adapter.list_summaries(ana, limit=2, offset=0)
    page2 = await adapter.list_summaries(ana, limit=2, offset=2)
    assert [c.id for c in page1] == [ids[2], ids[1]]  # last_message_at DESC
    assert [c.id for c in page2] == [ids[0]]
    assert page1[0].preview == "respuesta"  # latest CUSTOMER/ASSISTANT, TOOL ignored
    assert page1[1].preview == ""
    assert [c.title for c in await adapter.list_summaries(visitor, limit=20, offset=0)] == ["anon"]


async def test_retomar_una_conversacion_solo_para_su_dueno_y_en_orden_cronologico(postgres_session):
    """SPEC-05 Req. 1 · Scenario: Retomar una conversación."""
    session = postgres_session
    adapter, messages = ConversationPostgresAdapter(session), MessagePostgresAdapter(session)
    ana = CallerIdentity(customer_id=uuid.uuid4())
    other = CallerIdentity(customer_id=uuid.uuid4())
    cid = await titled(adapter, ana, "chat", 0)
    for i in range(3):
        await messages.guardar(Message(
            conversation_id=cid, role="CUSTOMER", content=f"m{i}", created_at=T0 + timedelta(minutes=i),
        ))
    assert await adapter.find_owned(cid, other) is None
    assert await adapter.find_owned(uuid.uuid4(), ana) is None
    recent = await messages.list_recent(cid, limit=2)
    assert [m.text for m in recent] == ["m1", "m2"]
    assert recent[0].role is MessageRole.CUSTOMER
