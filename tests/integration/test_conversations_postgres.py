"""Conversation queries against PostgreSQL 18 (needs TEST_DATABASE_URL, skipped otherwise)."""
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


async def say(messages, cid, role, content, minute, **extra):
    await messages.guardar(Message(
        conversation_id=cid, role=role, content=content, created_at=T0 + timedelta(minutes=minute), **extra
    ))


async def search(adapter, owner, q, *, limit=20, offset=0):
    return [c.id for c in await adapter.search_summaries(owner, q, limit=limit, offset=offset)]


async def test_buscar_por_titulo_o_por_mensaje_de_cliente_o_asistente(postgres_session):
    """SPEC-05 Req. 1 · Scenario: Buscar chats."""
    session = postgres_session
    adapter, messages = ConversationPostgresAdapter(session), MessagePostgresAdapter(session)
    ana = CallerIdentity(customer_id=uuid.uuid4())
    by_title = await titled(adapter, ana, "Zapatillas rojas", 0)
    by_customer = await titled(adapter, ana, "chat a", 1)
    by_assistant = await titled(adapter, ana, "chat b", 2)
    unrelated = await titled(adapter, ana, "chat c", 3)
    await say(messages, by_customer, "CUSTOMER", "busco zapatillas para correr", 0)
    await say(messages, by_assistant, "ASSISTANT", "Estas zapatillas son ideales", 0)
    await say(messages, unrelated, "CUSTOMER", "quiero una camisa", 0)

    assert await search(adapter, ana, "zapatillas") == [by_assistant, by_customer, by_title]
    found = await adapter.search_summaries(ana, "camisa", limit=20, offset=0)
    assert [c.id for c in found] == [unrelated] and found[0].preview == "quiero una camisa"


async def test_buscar_ignora_mensajes_tool(postgres_session):
    """SPEC-05 Req. 1 · Scenario: Buscar chats (tool payloads are internal)."""
    session = postgres_session
    adapter, messages = ConversationPostgresAdapter(session), MessagePostgresAdapter(session)
    ana = CallerIdentity(customer_id=uuid.uuid4())
    cid = await titled(adapter, ana, "chat", 0)
    await say(messages, cid, "TOOL", "resultado secreto del carrito", 0, tool_name="consultar_carrito")
    assert await search(adapter, ana, "secreto") == []


async def test_buscar_usa_la_configuracion_spanish_ignora_mayusculas_acentos_y_plurales(postgres_session):
    """SPEC-05 Req. 1 · Scenario: Buscar chats (behaviour of the 'spanish' text search configuration)."""
    session = postgres_session
    adapter, messages = ConversationPostgresAdapter(session), MessagePostgresAdapter(session)
    ana = CallerIdentity(customer_id=uuid.uuid4())
    cid = await titled(adapter, ana, "Camión de juguete", 0)
    await say(messages, cid, "CUSTOMER", "Quiero ZAPATILLAS", 0)
    assert await search(adapter, ana, "zapatillas") == [cid]  # case-insensitive
    assert await search(adapter, ana, "zapatilla") == [cid]  # stemming: singular finds plural
    assert await search(adapter, ana, "camión") == [cid]
    # The Snowball stemmer of 'spanish' folds accents, so both spellings find "Camión".
    assert await search(adapter, ana, "camion") == [cid]
    # Substrings do not match (full-text search works on lexemes, not on LIKE patterns).
    assert await search(adapter, ana, "zapat") == []


async def test_buscar_devuelve_una_vez_la_conversacion_con_varios_mensajes_coincidentes(postgres_session):
    """SPEC-05 Req. 1 · Scenario: Buscar chats (no duplicates)."""
    session = postgres_session
    adapter, messages = ConversationPostgresAdapter(session), MessagePostgresAdapter(session)
    ana = CallerIdentity(customer_id=uuid.uuid4())
    cid = await titled(adapter, ana, "zapatillas", 0)
    for i in range(3):
        await say(messages, cid, "CUSTOMER", f"zapatillas numero {i}", i)
    assert await search(adapter, ana, "zapatillas") == [cid]


async def test_buscar_aisla_por_dueno_cliente_y_visitante(postgres_session):
    """SPEC-05 Req. 1 · Scenario: Buscar chats (only the caller's conversations)."""
    session = postgres_session
    adapter, messages = ConversationPostgresAdapter(session), MessagePostgresAdapter(session)
    ana = CallerIdentity(customer_id=uuid.uuid4())
    bob = CallerIdentity(customer_id=uuid.uuid4())
    visitor = CallerIdentity(anonymous_sid="hash-visitor")
    other_visitor = CallerIdentity(anonymous_sid="hash-other")
    mine = await titled(adapter, ana, "zapatillas de ana", 0)
    theirs = await titled(adapter, bob, "zapatillas de bob", 1)
    anon = await titled(adapter, visitor, "chat anonimo", 2)
    await say(messages, anon, "CUSTOMER", "zapatillas", 0)
    await say(messages, theirs, "CUSTOMER", "zapatillas", 0)
    assert await search(adapter, ana, "zapatillas") == [mine]
    assert await search(adapter, bob, "zapatillas") == [theirs]
    assert await search(adapter, visitor, "zapatillas") == [anon]
    assert await search(adapter, other_visitor, "zapatillas") == []


async def test_buscar_ordena_por_actividad_y_pagina(postgres_session):
    """SPEC-05 Req. 1 · Scenario: Buscar chats (most recent activity first, paginated)."""
    session = postgres_session
    adapter = ConversationPostgresAdapter(session)
    ana = CallerIdentity(customer_id=uuid.uuid4())
    ids = [await titled(adapter, ana, f"zapatillas {i}", i) for i in range(3)]
    await titled(adapter, ana, "camisas", 9)
    assert await search(adapter, ana, "zapatillas", limit=2, offset=0) == [ids[2], ids[1]]
    assert await search(adapter, ana, "zapatillas", limit=2, offset=2) == [ids[0]]
    assert await search(adapter, ana, "zapatillas", limit=2, offset=4) == []


async def test_buscar_sin_resultados_y_con_sintaxis_hostil_no_falla(postgres_session):
    """SPEC-05 Req. 1 · Scenario: Buscar chats (no matches; input never reaches a raw tsquery)."""
    session = postgres_session
    adapter = ConversationPostgresAdapter(session)
    ana = CallerIdentity(customer_id=uuid.uuid4())
    await titled(adapter, ana, "zapatillas", 0)
    assert await search(adapter, ana, "abrigo") == []
    for hostile in ("'", "zapatillas &", "!!!", ":*", "a | ( b", "de"):
        await search(adapter, ana, hostile)  # must not raise
    assert await search(adapter, ana, "zapatillas -rojas") != []  # websearch syntax: '-' excludes only 'rojas'


async def test_buscar_omite_conversaciones_sin_titulo(postgres_session):
    """SPEC-05 Req. 1 · Scenario: Buscar chats (empty conversations are not listed, so not searched)."""
    session = postgres_session
    adapter, messages = ConversationPostgresAdapter(session), MessagePostgresAdapter(session)
    ana = CallerIdentity(customer_id=uuid.uuid4())
    untitled = await adapter.create(ana)
    await say(messages, untitled.id, "CUSTOMER", "zapatillas", 0)
    assert await search(adapter, ana, "zapatillas") == []
