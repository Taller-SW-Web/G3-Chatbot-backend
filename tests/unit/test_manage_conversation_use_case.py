"""H3-10: use case of SPEC-05 Req. 1, tested with fake adapters (ADR-0002)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.application.use_cases.manage_conversation_use_case import (
    CONVERSATIONS_PAGE_SIZE,
    HISTORY_LIMIT,
    ManageConversationUseCase,
)
from src.domain.entities.conversation import Conversation, ConversationSummary
from src.domain.entities.message import Message, MessageRole
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.inbound.chatbot_service_port import ConversationNotFoundError

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
ANA = CallerIdentity(customer_id=uuid.uuid4())
OTHER = CallerIdentity(anonymous_sid="hash-of-someone-else")


class FakeConversations:
    def __init__(self, fail_on_create=False):
        self.owned: dict[uuid.UUID, CallerIdentity] = {}
        self.summaries: list[ConversationSummary] = []
        self.fail_on_create = fail_on_create
        self.list_args = None

    async def create(self, owner):
        if self.fail_on_create:
            raise RuntimeError("db down")
        c = Conversation(id=uuid.uuid4(), title=None, last_message_at=NOW)
        self.owned[c.id] = owner
        return c

    async def list_summaries(self, owner, *, limit, offset):
        self.list_args = (owner, limit, offset)
        return self.summaries

    async def find_owned(self, conversation_id, owner):
        if self.owned.get(conversation_id) == owner:
            return Conversation(id=conversation_id, title="t", last_message_at=NOW)
        return None


class FakeMessages:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.limit = None

    async def list_recent(self, conversation_id, *, limit):
        self.limit = limit
        return self.messages


class FakeUow:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def build(conversations=None, messages=None):
    uow = FakeUow()
    conversations = conversations or FakeConversations()
    messages = messages or FakeMessages()
    return ManageConversationUseCase(conversations, messages, uow), conversations, messages, uow


def msg(role, text, minute=0):
    return Message(
        id=uuid.uuid4(), conversation_id=uuid.uuid4(), role=role, text=text, blocks=None,
        created_at=NOW + timedelta(minutes=minute),
    )


@pytest.mark.asyncio
async def test_nueva_conversacion_se_crea_vacia_y_se_confirma():
    """SPEC-05 Req. 1 · Scenario: Nueva conversación desde la barra lateral."""
    use_case, conversations, _, uow = build()
    created = await use_case.create_conversation(ANA)
    assert created.title is None  # sin mensajes: no aparece en el listado
    assert conversations.owned[created.id] == ANA
    assert (uow.commits, uow.rollbacks) == (1, 0)


@pytest.mark.asyncio
async def test_nueva_conversacion_revierte_si_falla_el_guardado():
    """SPEC-05 Req. 1 · Scenario: Nueva conversación desde la barra lateral (fallo)."""
    use_case, _, _, uow = build(FakeConversations(fail_on_create=True))
    with pytest.raises(RuntimeError):
        await use_case.create_conversation(ANA)
    assert (uow.commits, uow.rollbacks) == (0, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("page,offset", [(1, 0), (2, CONVERSATIONS_PAGE_SIZE), (0, 0)])
async def test_listado_de_conversaciones_recientes_pagina_de_20(page, offset):
    """SPEC-05 Req. 1 · Scenario: Listado de conversaciones recientes."""
    use_case, conversations, _, _ = build()
    summary = ConversationSummary(id=uuid.uuid4(), title="Zapatillas", preview="hola", last_message_at=NOW)
    conversations.summaries = [summary]
    assert await use_case.list_conversations(ANA, page) == [summary]
    assert conversations.list_args == (ANA, CONVERSATIONS_PAGE_SIZE, offset)


@pytest.mark.asyncio
async def test_listado_sin_identidad_devuelve_pagina_vacia():
    """SPEC-05 Req. 1 · Scenario: Listado de conversaciones recientes (visitante sin cookie)."""
    use_case, conversations, _, _ = build()
    assert await use_case.list_conversations(None, 1) == []
    assert conversations.list_args is None


@pytest.mark.asyncio
async def test_retomar_una_conversacion_devuelve_ultimos_50_sin_mensajes_tool():
    """SPEC-05 Req. 1 · Scenario: Retomar una conversación."""
    messages = FakeMessages([
        msg(MessageRole.CUSTOMER, "hola", 0),
        msg(MessageRole.TOOL, "", 1),
        msg(MessageRole.ASSISTANT, "buenas", 2),
    ])
    use_case, conversations, _, _ = build(messages=messages)
    cid = (await use_case.create_conversation(ANA)).id
    history = await use_case.get_history(ANA, cid)
    assert [m.text for m in history] == ["hola", "buenas"]
    assert messages.limit == HISTORY_LIMIT == 50


@pytest.mark.asyncio
async def test_retomar_conversacion_ajena_inexistente_o_sin_identidad_es_no_encontrada():
    """SPEC-05 Req. 1 · Scenario: Retomar una conversación (no es del solicitante)."""
    use_case, conversations, messages, _ = build()
    cid = (await use_case.create_conversation(ANA)).id
    for caller, target in [(OTHER, cid), (ANA, uuid.uuid4()), (None, cid)]:
        with pytest.raises(ConversationNotFoundError):
            await use_case.get_history(caller, target)
    assert messages.limit is None  # never read messages of a conversation that is not theirs
