"""H3-10: agent loop of SPEC-05 Req. 3 and 4 with the fake provider and fake tools (ADR-0002, ADR-0005)."""
import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from src.application.dto.conversation_events import (
    BlockEvent,
    DegradedEvent,
    FinalEvent,
    MessageAcceptedEvent,
    TokenEvent,
)
from src.application.use_cases.interpret_and_respond_use_case import (
    CONTEXT_MESSAGES,
    DEGRADED_TEXT,
    MAX_ITERATIONS,
    InterpretAndRespondUseCase,
    InvalidMessageError,
    derive_title,
)
from src.domain.entities.conversation import Conversation
from src.domain.entities.message import Message, MessageRole
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.inbound.chatbot_service_port import ConversationNotFoundError
from src.ports.outbound.llm_provider_port import (
    Completed,
    LLMRole,
    LLMTimeoutError,
    LLMUnavailableError,
    TextDelta,
    TextPart,
    TokenUsage,
    ToolCall,
    ToolCallRequested,
)
from src.ports.outbound.tool_executor_port import INVALID_ARGUMENTS, ToolResult
from tests.fakes.fake_llm_provider import FakeLLMProvider
from tests.fakes.fake_tool_executor import FakeToolExecutor

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
ANA = CallerIdentity(customer_id=uuid.uuid4())
OTHER = CallerIdentity(anonymous_sid="hash-of-someone-else")


class FakeConversations:
    def __init__(self, owner=ANA, title=None, summary=None):
        self.id = uuid.uuid4()
        self.owner = owner
        self.title = title
        self.summary = summary
        self.touches = 0

    async def find_owned(self, conversation_id, owner):
        if conversation_id == self.id and owner == self.owner:
            return Conversation(id=self.id, title=self.title, last_message_at=NOW, summary=self.summary)
        return None

    async def assign_title_if_missing(self, conversation_id, title):
        if self.title is not None:
            return False
        self.title = title
        return True

    async def touch(self, conversation_id):
        self.touches += 1


class FakeMessages:
    """Keeps every row; `list_dialogue` hides tool rows and honours the limit, like the real adapter."""

    def __init__(self, history=()):
        self.rows: list[dict] = []
        for role, text in history:
            self._append(role, text)

    def _append(self, role, text, **extra):
        row = {"id": uuid.uuid4(), "role": role, "text": text, **extra}
        self.rows.append(row)
        return row

    async def add(self, conversation_id, role, text, **extra):
        row = self._append(role, text, **extra)
        return Message(id=row["id"], conversation_id=conversation_id, role=role, text=text,
                       blocks=extra.get("blocks"), created_at=NOW)

    async def list_dialogue(self, conversation_id, *, limit):
        rows = [r for r in self.rows if r["role"] is not MessageRole.TOOL]
        return [Message(id=r["id"], conversation_id=conversation_id, role=r["role"], text=r["text"],
                        blocks=None, created_at=NOW) for r in rows[-limit:]]

    def by_role(self, role):
        return [r for r in self.rows if r["role"] is role]


class FakeUow:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def build(turns, *, tools=None, history=(), conversation=None, sanitize=None, timeout=5.0):
    conversation = conversation or FakeConversations()
    messages = FakeMessages(history)
    uow = FakeUow()
    llm = FakeLLMProvider(turns)
    tools = tools or FakeToolExecutor()
    use_case = InterpretAndRespondUseCase(
        conversation, messages, uow, llm, tools,
        assistant_name="Botleta", llm_timeout_seconds=timeout, sanitize_text=sanitize,
    )
    return use_case, conversation, messages, uow, llm, tools


async def run(use_case, conversation, text="hola", caller=ANA):
    return [e async for e in await use_case.send_message(caller, conversation.id, text)]


def reply(*texts, usage=None):
    return [*(TextDelta(t) for t in texts), Completed("stop", usage)]


def tool_turn(call_id, name, arguments):
    return [ToolCallRequested(ToolCall(call_id, name, arguments)), Completed("tool_calls")]


def kinds(events):
    return [type(e) for e in events]


@pytest.mark.asyncio
async def test_plain_reply_is_streamed_and_persisted():
    """SPEC-05 Req. 4 · Scenario: Envío y streaming normal."""
    uc, conv, messages, uow, _, _ = build([reply("Hola, ", "te ayudo", usage=TokenUsage(30, 7))])
    events = await run(uc, conv, "  quiero zapatillas ")
    assert kinds(events) == [MessageAcceptedEvent, TokenEvent, TokenEvent, FinalEvent]
    assert [e.text for e in events if isinstance(e, TokenEvent)] == ["Hola, ", "te ayudo"]
    final = events[-1].message
    assert final.role is MessageRole.ASSISTANT and final.text == "Hola, te ayudo"
    assert [(r["role"], r["text"]) for r in messages.rows] == [
        (MessageRole.CUSTOMER, "quiero zapatillas"), (MessageRole.ASSISTANT, "Hola, te ayudo"),
    ]
    assert (messages.rows[1]["input_tokens"], messages.rows[1]["output_tokens"]) == (30, 7)
    assert uow.commits == 2 and uow.rollbacks == 0  # customer message, then the reply


@pytest.mark.asyncio
async def test_tool_call_then_final_answer():
    """SPEC-05 Req. 3 · Scenario: Intención clara con parámetros."""
    block = {"type": "CARRUSEL_PRODUCTOS", "items": []}
    tools = FakeToolExecutor({"buscar_productos": ToolResult("OK", {"total": 2}, block=block)})
    uc, conv, messages, _, llm, tools = build(
        [tool_turn("c1", "buscar_productos", {"marca": "Nike"}), reply("Encontré 2 opciones")], tools=tools
    )
    events = await run(uc, conv, "zapatillas Nike")
    assert kinds(events) == [MessageAcceptedEvent, BlockEvent, TokenEvent, FinalEvent]
    assert events[1].block == block
    assert tools.executed[0][0:2] == ("buscar_productos", {"marca": "Nike"})
    assert tools.executed[0][2].customer_id == ANA.customer_id  # identity from the caller, not the model
    # second LLM call sees the assistant tool call and the delimited tool result
    second = llm.calls[1][0]
    assert second[-2].tool_calls[0].id == "c1"
    assert second[-1].role is LLMRole.TOOL and second[-1].tool_call_id == "c1"
    assert second[-1].content[0].text.startswith("<datos_herramienta>")
    [tool_row] = messages.by_role(MessageRole.TOOL)
    assert (tool_row["tool_name"], tool_row["arguments"], tool_row["result_code"]) == (
        "buscar_productos", {"marca": "Nike"}, "OK")
    assert messages.by_role(MessageRole.ASSISTANT)[0]["blocks"] == {"blocks": [block]}


@pytest.mark.asyncio
async def test_tool_error_is_fed_back_to_the_model():
    """SPEC-05 Req. 6 · Scenario: Herramienta protegida sin sesión."""
    tools = FakeToolExecutor({"listar_pedidos": ToolResult("REQUIERE_SESION", is_error=True)})
    uc, conv, messages, _, llm, _ = build(
        [tool_turn("c1", "listar_pedidos", {}), reply("Primero inicia sesión")], tools=tools
    )
    events = await run(uc, conv)
    assert isinstance(events[-1], FinalEvent) and events[-1].message.text == "Primero inicia sesión"
    fed_back = llm.calls[1][0][-1].content[0].text
    assert "REQUIERE_SESION" in fed_back and '"error": true' in fed_back
    assert messages.by_role(MessageRole.TOOL)[0]["result_code"] == "REQUIERE_SESION"


@pytest.mark.asyncio
async def test_crashing_tool_is_reported_to_the_model_as_a_generic_error():
    def boom(_):
        raise RuntimeError("secret internals")

    uc, conv, _, _, llm, _ = build(
        [tool_turn("c1", "buscar_productos", {}), reply("Hubo un problema")],
        tools=FakeToolExecutor({"buscar_productos": boom}),
    )
    events = await run(uc, conv)
    assert isinstance(events[-1], FinalEvent)
    fed_back = llm.calls[1][0][-1].content[0].text
    assert "ERROR_INTERNO" in fed_back and "secret internals" not in fed_back


@pytest.mark.asyncio
async def test_iteration_cap_ends_with_a_safe_fallback_and_skips_the_last_tool_calls():
    """ADR-0005: at most 5 LLM iterations per customer message."""
    tools = FakeToolExecutor({"ver_carrito": ToolResult("OK")})
    uc, conv, messages, _, llm, tools = build(
        [tool_turn(f"c{i}", "ver_carrito", {}) for i in range(MAX_ITERATIONS)], tools=tools
    )
    events = await run(uc, conv)
    assert len(llm.calls) == MAX_ITERATIONS == 5
    assert len(tools.executed) == MAX_ITERATIONS - 1  # the 5th request has no iteration left to use its result
    assert DegradedEvent("iteration_limit") in events
    final = events[-1]
    assert isinstance(final, FinalEvent) and "reformularla" in final.message.text
    assert messages.by_role(MessageRole.ASSISTANT)[0]["text"] == final.message.text


@pytest.mark.asyncio
async def test_repeated_invalid_arguments_stop_after_one_retry():
    """SPEC-05 Req. 6 · Scenario: Argumentos inválidos del LLM (máx. 1 reintento)."""
    tools = FakeToolExecutor({"cambiar_cantidad": ToolResult(INVALID_ARGUMENTS, is_error=True)})
    uc, conv, _, _, llm, tools = build(
        [tool_turn("c1", "cambiar_cantidad", {"cantidad": -3}),
         tool_turn("c2", "cambiar_cantidad", {"cantidad": -3}), reply("nunca")],
        tools=tools,
    )
    events = await run(uc, conv)
    assert len(llm.calls) == 2 and len(tools.executed) == 2
    assert DegradedEvent("invalid_arguments") in events and isinstance(events[-1], FinalEvent)


@pytest.mark.asyncio
@pytest.mark.parametrize("error, reason", [
    (LLMTimeoutError("slow"), "timeout"), (LLMUnavailableError("down"), "unavailable"),
])
async def test_provider_failure_keeps_the_customer_message_and_replies_degraded(error, reason):
    """SPEC-05 Req. 10 · Scenario: El LLM no responde."""
    uc, conv, messages, uow, _, _ = build([error])
    events = await run(uc, conv, "zapatillas")
    assert kinds(events) == [MessageAcceptedEvent, DegradedEvent, BlockEvent, FinalEvent]
    assert events[1] == DegradedEvent(reason)
    assert events[2].block["tipo"] == "ACCIONES_RAPIDAS" and len(events[2].block["acciones"]) == 6
    assert events[-1].message.text == DEGRADED_TEXT
    assert [r["text"] for r in messages.rows] == ["zapatillas", DEGRADED_TEXT]
    assert uow.rollbacks == 0 and uow.commits == 2


@pytest.mark.asyncio
async def test_failure_mid_stream_replaces_the_partial_text_with_the_fallback():
    class Breaking(FakeLLMProvider):
        async def stream(self, messages, tools):
            yield TextDelta("Hola, te")
            raise LLMUnavailableError("connection reset")

    uc, conv, messages, _, _, _ = build([])
    uc._llm = Breaking([])
    events = await run(uc, conv)
    assert kinds(events)[:3] == [MessageAcceptedEvent, TokenEvent, DegradedEvent]
    assert messages.by_role(MessageRole.ASSISTANT)[0]["text"] == DEGRADED_TEXT


@pytest.mark.asyncio
async def test_a_stalled_provider_is_cut_by_the_overall_deadline():
    class Stalled(FakeLLMProvider):
        async def stream(self, messages, tools):
            yield TextDelta("a")
            await asyncio.sleep(10)
            yield Completed("stop")

    uc, conv, messages, _, _, _ = build([], timeout=0.05)
    uc._llm = Stalled([])
    events = await asyncio.wait_for(run(uc, conv), 2)
    assert DegradedEvent("timeout") in events
    assert [r["role"] for r in messages.rows] == [MessageRole.CUSTOMER, MessageRole.ASSISTANT]


@pytest.mark.asyncio
async def test_empty_model_answer_degrades():
    uc, conv, _, _, _, _ = build([[Completed("stop")]])
    events = await run(uc, conv)
    assert DegradedEvent("unavailable") in events and events[-1].message.text == DEGRADED_TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [OTHER, None])
async def test_foreign_missing_or_anonymous_conversation_is_not_found_and_stores_nothing(owner):
    """SPEC-05 Req. 1 · Scenario: Retomar una conversación (no es del solicitante)."""
    uc, conv, messages, uow, llm, _ = build([reply("x")])
    with pytest.raises(ConversationNotFoundError):
        await uc.send_message(owner, conv.id, "hola")
    with pytest.raises(ConversationNotFoundError):
        await uc.send_message(ANA, uuid.uuid4(), "hola")
    assert messages.rows == [] and uow.commits == 0 and llm.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "   ", "x" * 1001])
async def test_blank_or_oversized_message_is_rejected(text):
    uc, conv, messages, _, _, _ = build([reply("x")])
    with pytest.raises(InvalidMessageError):
        await uc.send_message(ANA, conv.id, text)
    assert messages.rows == []


@pytest.mark.asyncio
async def test_title_is_assigned_only_on_the_first_message():
    """SPEC-05 Req. 1 · Scenario: Listado (título derivado del primer mensaje, máx. 40 caracteres)."""
    uc, conv, _, _, _, _ = build([reply("uno"), reply("dos")])
    await run(uc, conv, "Busco   zapatillas Nike para correr de menos de 350 soles")
    assert conv.title == "Busco zapatillas Nike para correr de men" and len(conv.title) <= 40
    await run(uc, conv, "otra cosa")
    assert conv.title == "Busco zapatillas Nike para correr de men"
    assert derive_title("hola\n mundo") == "hola mundo"


@pytest.mark.asyncio
async def test_an_existing_title_is_never_replaced():
    uc, conv, _, _, _, _ = build([reply("ok")], conversation=FakeConversations(title="Ya tenía título"))
    await run(uc, conv, "mensaje nuevo")
    assert conv.title == "Ya tenía título"


@pytest.mark.asyncio
async def test_context_is_summary_plus_last_12_messages_without_tool_rows():
    """ADR-0005 / SPEC-05 RNF Contexto."""
    history = [(MessageRole.CUSTOMER if i % 2 == 0 else MessageRole.ASSISTANT, f"m{i}") for i in range(30)]
    history.insert(5, (MessageRole.TOOL, "tool row"))
    conv = FakeConversations(title="t", summary="El cliente busca zapatillas")
    uc, conv, _, _, llm, _ = build([reply("ok")], history=history, conversation=conv)
    await run(uc, conv, "nuevo")
    sent = llm.calls[0][0]
    assert sent[0].role is LLMRole.SYSTEM
    assert "El cliente busca zapatillas" in sent[0].content[0].text
    texts = [m.content[0].text for m in sent[1:]]
    assert len(texts) == CONTEXT_MESSAGES == 12
    assert texts[-1] == "nuevo" and texts[0] == "m19" and "tool row" not in texts


@pytest.mark.asyncio
async def test_system_prompt_introduces_the_assistant_only_on_the_first_reply():
    """SPEC-05 Req. 12 · Scenario: Presentación en la primera respuesta."""
    uc, conv, _, _, llm, _ = build([reply("a")])
    await run(uc, conv)
    assert "primera respuesta" in llm.calls[0][0][0].content[0].text
    assert "Botleta" in llm.calls[0][0][0].content[0].text
    uc, conv, _, _, llm, _ = build([reply("b")], history=[(MessageRole.CUSTOMER, "hi"), (MessageRole.ASSISTANT, "hello")])
    await run(uc, conv)
    assert "Primera respuesta" not in llm.calls[0][0][0].content[0].text


@pytest.mark.asyncio
async def test_sanitizer_runs_before_persisting_prompting_and_titling():
    """SPEC-05 Req. 9: the filter of the tools module plugs in as `sanitize_text`."""
    from src.domain.services.sensitive_data_filter import SensitiveDataFilter

    uc, conv, messages, _, llm, _ = build(
        [reply("ok")], sanitize=lambda t: SensitiveDataFilter().redactar(t).texto
    )
    text = "mi tarjeta 4111111111111111 y mi contraseña es Secreta123!"
    expected = "mi tarjeta [tarjeta oculta] y mi contraseña es [contraseña oculta]!"
    await run(uc, conv, text)
    assert messages.rows[0]["text"] == expected
    assert llm.calls[0][0][-1].content == (TextPart(expected),)
    assert conv.title == expected[:40]


@pytest.mark.asyncio
async def test_unexpected_persistence_failure_rolls_back_and_raises():
    uc, conv, messages, uow, _, _ = build([reply("x")])
    original_add = messages.add

    async def failing_add(conversation_id, role, text, **extra):
        if role is MessageRole.ASSISTANT:
            raise RuntimeError("db down")
        return await original_add(conversation_id, role, text, **extra)

    messages.add = failing_add
    with pytest.raises(RuntimeError):
        await run(uc, conv)
    assert uow.rollbacks == 1 and uow.commits == 1  # customer message stayed committed


@pytest.mark.asyncio
async def test_degraded_reply_keeps_blocks_of_executed_tools_before_the_quick_actions():
    block = {"type": "CARRUSEL_PRODUCTOS", "items": []}
    tools = FakeToolExecutor({"buscar_productos": ToolResult("OK", block=block)})
    uc, conv, messages, _, _, _ = build(
        [tool_turn("c1", "buscar_productos", {}), LLMUnavailableError("down")], tools=tools
    )
    events = await run(uc, conv)
    streamed = [e.block for e in events if isinstance(e, BlockEvent)]
    stored = messages.by_role(MessageRole.ASSISTANT)[0]["blocks"]["blocks"]
    assert streamed == stored and stored[0] == block and stored[1]["tipo"] == "ACCIONES_RAPIDAS"


@pytest.mark.asyncio
async def test_closing_the_stream_after_a_tool_ran_commits_its_row():
    tools = FakeToolExecutor({"vaciar_carrito": ToolResult("OK", block={"type": "CARRITO"})})
    uc, conv, messages, uow, _, _ = build([tool_turn("c1", "vaciar_carrito", {}), reply("listo")], tools=tools)
    stream = await uc.send_message(ANA, conv.id, "vacía mi carrito")
    seen = []
    async for event in stream:
        seen.append(event)
        if isinstance(event, BlockEvent):
            break
    await stream.aclose()
    assert len(messages.by_role(MessageRole.TOOL)) == 1
    assert uow.commits == 2 and uow.rollbacks == 0  # customer message, then the executed tool row
    assert messages.by_role(MessageRole.ASSISTANT) == []


@pytest.mark.asyncio
async def test_closing_the_stream_before_any_tool_ran_rolls_back():
    uc, conv, messages, uow, _, _ = build([reply("hola")])
    stream = await uc.send_message(ANA, conv.id, "hola")
    await stream.__anext__()  # MessageAcceptedEvent
    await stream.aclose()
    assert uow.commits == 1 and uow.rollbacks == 1
    assert [r["role"] for r in messages.rows] == [MessageRole.CUSTOMER]


@pytest.mark.asyncio
async def test_cancelling_the_turn_ends_the_unit_of_work_and_is_not_swallowed():
    class Stalled(FakeLLMProvider):
        async def stream(self, messages, tools):
            await asyncio.sleep(10)
            yield Completed("stop")

    uc, conv, _, uow, _, _ = build([], timeout=30)
    uc._llm = Stalled([])
    task = asyncio.ensure_future(run(uc, conv))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert uow.rollbacks == 1
