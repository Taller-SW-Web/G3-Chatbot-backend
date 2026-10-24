"""Message writes and agent-loop persistence against PostgreSQL 18 (needs TEST_DATABASE_URL)."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.adapters.outbound.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)
from src.adapters.outbound.persistence.message_postgres_adapter import MessagePostgresAdapter
from src.adapters.outbound.persistence.models import Conversation
from src.adapters.outbound.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.application.dto.conversation_events import FinalEvent
from src.application.use_cases.interpret_and_respond_use_case import InterpretAndRespondUseCase
from src.application.use_cases.manage_conversation_use_case import ManageConversationUseCase
from src.domain.entities.message import MessageRole
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.outbound.llm_provider_port import Completed, TextDelta, ToolCall, ToolCallRequested
from src.ports.outbound.tool_executor_port import ToolResult
from tests.fakes.fake_llm_provider import FakeLLMProvider
from tests.fakes.fake_tool_executor import FakeToolExecutor

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
ANA = CallerIdentity(customer_id=uuid.uuid4())


async def titled(adapter, owner, title, minute):
    row = await adapter.guardar(Conversation(
        customer_id=owner.customer_id, anonymous_sid=owner.anonymous_sid, title=title,
        last_message_at=T0 + timedelta(minutes=minute),
    ))
    return row.id


async def test_add_persists_customer_assistant_and_tool_rows(postgres_session):
    """SPEC-05 Req. 1 and 3: message rows written by the agent loop."""
    conversations, messages = ConversationPostgresAdapter(postgres_session), MessagePostgresAdapter(postgres_session)
    cid = (await conversations.create(ANA)).id
    customer = await messages.add(cid, MessageRole.CUSTOMER, "zapatillas")
    tool = await messages.add(
        cid, MessageRole.TOOL, '{"code": "OK"}',
        tool_name="buscar_productos", arguments={"marca": "Nike"}, result_code="OK",
    )
    block = {"blocks": [{"type": "CARRUSEL_PRODUCTOS", "items": []}]}
    assistant = await messages.add(
        cid, MessageRole.ASSISTANT, "Encontré dos", blocks=block,
        input_tokens=30, output_tokens=7, latency_ms=420,
    )
    assert (customer.role, tool.role, assistant.role) == (
        MessageRole.CUSTOMER, MessageRole.TOOL, MessageRole.ASSISTANT)
    assert assistant.blocks == block and customer.blocks is None

    from sqlalchemy import select

    from src.adapters.outbound.persistence.models import Message
    rows = {r.role: r for r in (await postgres_session.execute(select(Message))).scalars()}
    assert (rows["TOOL"].tool_name, rows["TOOL"].arguments, rows["TOOL"].result_code) == (
        "buscar_productos", {"marca": "Nike"}, "OK")
    assert (rows["ASSISTANT"].input_tokens, rows["ASSISTANT"].output_tokens, rows["ASSISTANT"].latency_ms) == (
        30, 7, 420)

    # History keeps hiding TOOL rows (existing use case behaviour).
    history = await ManageConversationUseCase(
        conversations, messages, SqlAlchemyUnitOfWork(postgres_session)
    ).get_history(ANA, cid)
    assert [m.role for m in history] == [MessageRole.CUSTOMER, MessageRole.ASSISTANT]


async def test_blocks_with_decimal_prices_are_stored_like_the_sse_frame(postgres_session):
    """Catalog blocks carry Decimal prices; JSONB stores them as strings, as `sse.frame` sends them."""
    conversations, messages = ConversationPostgresAdapter(postgres_session), MessagePostgresAdapter(postgres_session)
    cid = (await conversations.create(ANA)).id
    block = {"blocks": [{
        "tipo": "CARRUSEL_PRODUCTOS",
        "datos": {"productos": [{"price": Decimal("279.90"), "price_text": "S/ 279.90"}]},
    }]}

    await messages.add(cid, MessageRole.ASSISTANT, "Encontré uno", blocks=block)
    await postgres_session.flush()

    from sqlalchemy import select

    from src.adapters.outbound.persistence.models import Message
    stored = (await postgres_session.execute(
        select(Message.blocks).where(Message.conversation_id == cid)
    )).scalar_one()
    assert stored["blocks"][0]["datos"]["productos"][0]["price"] == "279.90"


async def test_rows_added_in_one_transaction_keep_insertion_order(postgres_session):
    conversations, messages = ConversationPostgresAdapter(postgres_session), MessagePostgresAdapter(postgres_session)
    cid = (await conversations.create(ANA)).id
    for i in range(5):
        await messages.add(cid, MessageRole.CUSTOMER, f"m{i}")
    assert [m.text for m in await messages.list_recent(cid, limit=5)] == [f"m{i}" for i in range(5)]


async def test_list_dialogue_returns_latest_customer_and_assistant_rows_in_order(postgres_session):
    conversations, messages = ConversationPostgresAdapter(postgres_session), MessagePostgresAdapter(postgres_session)
    cid = (await conversations.create(ANA)).id
    other = (await conversations.create(ANA)).id
    await messages.add(other, MessageRole.CUSTOMER, "other conversation")
    for i in range(4):
        await messages.add(cid, MessageRole.CUSTOMER, f"q{i}")
        await messages.add(cid, MessageRole.TOOL, "{}", tool_name="t", result_code="OK")
        await messages.add(cid, MessageRole.ASSISTANT, f"a{i}")
    dialogue = await messages.list_dialogue(cid, limit=5)
    assert [m.text for m in dialogue] == ["a1", "q2", "a2", "q3", "a3"]
    assert all(m.role is not MessageRole.TOOL for m in dialogue)
    assert len(await messages.list_dialogue(cid, limit=100)) == 8  # no 50-row cap like list_recent


async def test_assign_title_if_missing_sets_it_once(postgres_session):
    """SPEC-05 Req. 1: the title comes from the first message only."""
    adapter = ConversationPostgresAdapter(postgres_session)
    cid = (await adapter.create(ANA)).id
    assert await adapter.assign_title_if_missing(cid, "primero") is True
    assert await adapter.assign_title_if_missing(cid, "segundo") is False
    assert (await adapter.find_owned(cid, ANA)).title == "primero"


async def test_touch_moves_the_conversation_to_the_top_of_the_listing(postgres_session):
    adapter = ConversationPostgresAdapter(postgres_session)
    old, newer = await titled(adapter, ANA, "old", 0), await titled(adapter, ANA, "newer", 10)
    assert [c.id for c in await adapter.list_summaries(ANA, limit=20, offset=0)] == [newer, old]
    await adapter.touch(old)
    assert [c.id for c in await adapter.list_summaries(ANA, limit=20, offset=0)] == [old, newer]


async def test_find_owned_exposes_the_summary(postgres_session):
    adapter = ConversationPostgresAdapter(postgres_session)
    row = await adapter.guardar(Conversation(customer_id=ANA.customer_id, summary="busca zapatillas"))
    assert (await adapter.find_owned(row.id, ANA)).summary == "busca zapatillas"


async def test_agent_loop_end_to_end_persists_the_turn(postgres_session):
    """SPEC-05 Req. 3 and 4: customer row, tool row and assistant row, with title and ordering updated."""
    session = postgres_session
    conversations, messages = ConversationPostgresAdapter(session), MessagePostgresAdapter(session)
    cid = (await conversations.create(ANA)).id
    block = {"type": "CARRUSEL_PRODUCTOS", "items": []}
    use_case = InterpretAndRespondUseCase(
        conversations, messages, SqlAlchemyUnitOfWork(session),
        FakeLLMProvider([
            [ToolCallRequested(ToolCall("c1", "buscar_productos", {"marca": "Nike"})), Completed("tool_calls")],
            [TextDelta("Encontré "), TextDelta("dos opciones"), Completed("stop")],
        ]),
        FakeToolExecutor({"buscar_productos": ToolResult("OK", {"total": 2}, blocks=(block,))}),
        assistant_name="Botleta", llm_timeout_seconds=5,
    )
    events = [e async for e in await use_case.send_message(ANA, cid, "zapatillas Nike")]
    assert isinstance(events[-1], FinalEvent) and events[-1].message.text == "Encontré dos opciones"

    stored = await messages.list_recent(cid, limit=50)
    assert [(m.role, m.text) for m in stored if m.role is not MessageRole.TOOL] == [
        (MessageRole.CUSTOMER, "zapatillas Nike"), (MessageRole.ASSISTANT, "Encontré dos opciones")]
    assert [m.role for m in stored] == [MessageRole.CUSTOMER, MessageRole.TOOL, MessageRole.ASSISTANT]
    assert stored[-1].blocks == {"blocks": [block]}
    conversation = await conversations.find_owned(cid, ANA)
    assert conversation.title == "zapatillas Nike"
    assert [s.id for s in await conversations.list_summaries(ANA, limit=20, offset=0)] == [cid]
