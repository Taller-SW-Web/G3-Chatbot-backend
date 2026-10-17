"""Conversation engine core (SPEC-05 Req. 3 and 4, ADR-0005): one customer message in, a stream of events out.

Phase 1 (awaiting `send_message`) validates ownership and persists the customer message, so it survives any
later LLM failure. Phase 2 (iterating the result) runs the LLM -> tool -> LLM loop and persists the reply.
"""
import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from src.application.dto.conversation_events import (
    BlockEvent,
    ConversationEvent,
    DegradedEvent,
    DegradedReason,
    FinalEvent,
    MessageAcceptedEvent,
    TokenEvent,
)
from src.application.prompts.system_prompt import (
    TOOL_DATA_CLOSE,
    TOOL_DATA_OPEN,
    build_summary_message,
    build_system_prompt,
)
from src.domain.entities.conversation import Conversation
from src.domain.entities.message import Message, MessageRole
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.inbound.chatbot_service_port import ConversationNotFoundError
from src.ports.outbound.conversation_repository_port import ConversationRepositoryPort
from src.ports.outbound.llm_provider_port import (
    Completed,
    LLMError,
    LLMEvent,
    LLMMessage,
    LLMProvider,
    LLMRole,
    LLMTimeoutError,
    LLMUnavailableError,
    TextDelta,
    TextPart,
    ToolCall,
    ToolCallRequested,
    ToolDefinition,
)
from src.ports.outbound.message_repository_port import MessageRepositoryPort
from src.ports.outbound.tool_executor_port import (
    INVALID_ARGUMENTS,
    ToolExecutionContext,
    ToolExecutorPort,
    ToolResult,
)
from src.ports.outbound.unit_of_work_port import UnitOfWorkPort

logger = logging.getLogger(__name__)

CONTEXT_MESSAGES = 12  # ADR-0005: summary + last 12 messages
MAX_ITERATIONS = 5  # ADR-0005: LLM calls per customer message
MAX_VALIDATION_FAILURES = 1  # SPEC-05 Req. 6: invalid tool arguments get at most 1 retry
TITLE_MAX_CHARS = 40  # SPEC-05 Req. 1 (matches ck_conversation_title_length)
MAX_MESSAGE_CHARS = 1000  # Composer limit (SPEC-05 design.md, frontend)
TOOL_RESULT_MAX_CHARS = 6000  # provisional cap on what one tool result adds to the context

# Spec text of the degraded reply and its quick actions (SPEC-05 Req. 10).
DEGRADED_TEXT = "Estoy teniendo problemas para entenderte, pero puedes usar estas opciones"
QUICK_ACTIONS = (  # (action id, label); ids are provisional, the spec only names VER_OFERTAS
    ("BUSCAR_POR_CATEGORIA", "Buscar por categoría"),
    ("VER_OFERTAS", "Ofertas"),
    ("VER_CARRITO", "Carrito"),
    ("MIS_PEDIDOS", "Mis pedidos"),
    ("MIS_RECLAMOS", "Mis reclamos"),
    ("MIS_DEVOLUCIONES", "Mis devoluciones"),
)
# Provisional copy: the spec only gives the text for the persistent-validation-error case as an example.
ITERATION_LIMIT_TEXT = (
    "No pude completar tu solicitud. ¿Puedes reformularla o elegir una de estas opciones?"
)
INVALID_ARGUMENTS_TEXT = "No entendí bien lo que necesitas, ¿puedes darme más detalle?"

_WHITESPACE = re.compile(r"\s+")


class InvalidMessageError(ValueError):
    """Blank or too long customer message."""


def derive_title(text: str) -> str:
    """Title of a conversation: the first customer message, whitespace collapsed, at most 40 characters."""
    return _WHITESPACE.sub(" ", text).strip()[:TITLE_MAX_CHARS].rstrip()


class InterpretAndRespondUseCase:
    def __init__(
        self,
        conversations: ConversationRepositoryPort,
        messages: MessageRepositoryPort,
        uow: UnitOfWorkPort,
        llm: LLMProvider,
        tools: ToolExecutorPort,
        *,
        assistant_name: str,
        llm_timeout_seconds: float,
        sanitize_text: Callable[[str], str] | None = None,
    ):
        """`sanitize_text` redacts sensitive data (SPEC-05 Req. 9) before persistence or prompting.

        The production container supplies SensitiveDataFilter. The identity default keeps direct
        constructions explicit and convenient for tests and adapters that do not use the container.
        """
        self._conversations = conversations
        self._messages = messages
        self._uow = uow
        self._llm = llm
        self._tools = tools
        self._assistant_name = assistant_name
        self._timeout = llm_timeout_seconds
        self._sanitize = sanitize_text or (lambda text: text)

    async def send_message(
        self, caller: CallerIdentity | None, conversation_id: uuid.UUID, text: str
    ) -> AsyncIterator[ConversationEvent]:
        """Raises ConversationNotFoundError or InvalidMessageError. Awaiting persists the customer message;
        iterating the returned stream produces the assistant turn."""
        text = (text or "").strip()
        if not text or len(text) > MAX_MESSAGE_CHARS:
            raise InvalidMessageError(f"message must have 1 to {MAX_MESSAGE_CHARS} characters")
        if caller is None:
            raise ConversationNotFoundError(conversation_id)
        conversation = await self._conversations.find_owned(conversation_id, caller)
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)

        clean = self._sanitize(text)
        try:
            customer_message = await self._messages.add(conversation_id, MessageRole.CUSTOMER, clean)
            if conversation.title is None:  # first customer message of the conversation
                await self._conversations.assign_title_if_missing(conversation_id, derive_title(clean))
            await self._conversations.touch(conversation_id)
            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise
        return self._run_turn(caller, conversation, customer_message)

    async def _run_turn(
        self, caller: CallerIdentity, conversation: Conversation, customer_message: Message
    ) -> AsyncIterator[ConversationEvent]:
        started = time.monotonic()
        turn = _Turn()
        try:
            yield MessageAcceptedEvent(customer_message)
            async for event in self._loop(caller, conversation, turn, started):
                yield event
        except (GeneratorExit, asyncio.CancelledError):
            # Consumer gone (WebSocket closed) or task cancelled: leave the unit of work in a defined state.
            # Tool rows record real side effects, so they are kept; with nothing executed there is nothing to keep.
            if turn.tool_names:
                await self._uow.commit()
            else:
                await self._uow.rollback()
            raise

    async def _loop(
        self, caller: CallerIdentity, conversation: Conversation, turn: "_Turn", started: float
    ) -> AsyncIterator[ConversationEvent]:
        try:
            dialogue = await self._messages.list_dialogue(conversation.id, limit=CONTEXT_MESSAGES)
            context = self._build_context(conversation, dialogue)
            tool_definitions = self._tools.definitions()
            execution = ToolExecutionContext(
                conversation_id=conversation.id,
                customer_id=caller.customer_id,
                anonymous_sid=caller.anonymous_sid,
            )
            degraded: DegradedReason | None = None
            try:
                for iteration in range(MAX_ITERATIONS):
                    iteration_text: list[str] = []
                    calls: list[ToolCall] = []
                    async for event in self._stream_llm(context, tool_definitions):
                        if isinstance(event, TextDelta):
                            iteration_text.append(event.text)
                            yield TokenEvent(event.text)
                        elif isinstance(event, ToolCallRequested):
                            calls.append(event.call)
                        elif isinstance(event, Completed) and event.usage is not None:
                            turn.input_tokens += event.usage.prompt_tokens
                            turn.output_tokens += event.usage.completion_tokens
                    turn.text_parts.extend(iteration_text)

                    if not calls:
                        if not "".join(turn.text_parts).strip():
                            degraded = "unavailable"  # the model answered nothing usable
                        break
                    if iteration == MAX_ITERATIONS - 1:
                        degraded = "iteration_limit"  # requested tools are not executed: no iteration left
                        break

                    context.append(
                        LLMMessage(
                            role=LLMRole.ASSISTANT,
                            content=(TextPart("".join(iteration_text)),) if iteration_text else (),
                            tool_calls=tuple(calls),
                        )
                    )
                    for call in calls:
                        result = await self._execute_tool(call, execution, turn, conversation.id)
                        context.append(
                            LLMMessage(
                                role=LLMRole.TOOL,
                                content=(TextPart(_wrap_tool_data(_serialize(result))),),
                                tool_call_id=call.id,
                            )
                        )
                        for tool_block in result.blocks:
                            block = dict(tool_block)
                            turn.blocks.append(block)
                            yield BlockEvent(block)
                    if turn.validation_failures > MAX_VALIDATION_FAILURES:
                        degraded = "invalid_arguments"
                        break
            except LLMTimeoutError:
                degraded = "timeout"
            except LLMError:
                degraded = "unavailable"

            if degraded is not None:
                reply, fallback_blocks = self._degraded_reply(degraded)
                logger.warning("conversation %s turn degraded: %s", conversation.id, degraded)
                yield DegradedEvent(degraded)
                # The client drops the tokens it had before `degradado`; the fallback text is streamed after
                # it so the live message matches the stored reply.
                yield TokenEvent(reply)
                for block in fallback_blocks:
                    yield BlockEvent(block)
                # Blocks of tools that already ran were streamed, so they stay in the stored reply too.
                blocks = [*turn.blocks, *fallback_blocks]
                final = await self._persist_reply(conversation.id, reply, blocks, turn, started)
            else:
                final = await self._persist_reply(
                    conversation.id, "".join(turn.text_parts), turn.blocks, turn, started
                )
        except Exception:
            await self._uow.rollback()
            raise
        logger.info(
            "conversation %s turn done: tools=%s in=%s out=%s latency_ms=%s",
            conversation.id,
            turn.tool_names,
            turn.input_tokens,
            turn.output_tokens,
            final.latency_ms,
        )
        yield FinalEvent(final.message)

    def _build_context(self, conversation: Conversation, dialogue: Sequence[Message]) -> list[LLMMessage]:
        first_reply = not any(m.role is MessageRole.ASSISTANT for m in dialogue)
        system = build_system_prompt(self._assistant_name, first_reply=first_reply)
        if conversation.summary:
            system += "\n" + build_summary_message(conversation.summary)
        context = [LLMMessage(role=LLMRole.SYSTEM, content=(TextPart(system),))]
        for message in dialogue:
            if not message.text:
                continue
            role = LLMRole.USER if message.role is MessageRole.CUSTOMER else LLMRole.ASSISTANT
            context.append(LLMMessage(role=role, content=(TextPart(message.text),)))
        return context

    async def _stream_llm(
        self, messages: Sequence[LLMMessage], tools: Sequence[ToolDefinition]
    ) -> AsyncIterator[LLMEvent]:
        """One provider call under a strict deadline. Only time spent waiting on the provider counts,
        not the time the consumer takes between events."""
        remaining = self._timeout
        iterator = self._llm.stream(messages, tools).__aiter__()
        try:
            while True:
                if remaining <= 0:
                    raise LLMTimeoutError("LLM call exceeded the configured deadline")
                waited_from = time.monotonic()
                try:
                    event = await asyncio.wait_for(iterator.__anext__(), remaining)
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    raise LLMTimeoutError("LLM call exceeded the configured deadline") from None
                remaining -= time.monotonic() - waited_from
                yield event
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()

    async def _execute_tool(
        self, call: ToolCall, execution: ToolExecutionContext, turn: "_Turn", conversation_id: uuid.UUID
    ) -> ToolResult:
        try:
            result = await self._tools.execute(call.name, call.arguments, execution)
        except Exception:
            # Unexpected tool failure: the model hears a generic error; details stay in the log.
            logger.exception("tool %s failed in conversation %s", call.name, conversation_id)
            result = ToolResult(code="ERROR_INTERNO", is_error=True)
        if result.code == INVALID_ARGUMENTS:
            turn.validation_failures += 1
        turn.tool_names.append(call.name)
        await self._messages.add(
            conversation_id,
            MessageRole.TOOL,
            _serialize(result),
            tool_name=call.name,
            arguments=dict(call.arguments),
            result_code=result.code,
        )
        return result

    @staticmethod
    def _degraded_reply(reason: DegradedReason) -> tuple[str, list[dict[str, Any]]]:
        if reason == "invalid_arguments":
            return INVALID_ARGUMENTS_TEXT, []
        text = ITERATION_LIMIT_TEXT if reason == "iteration_limit" else DEGRADED_TEXT
        block = {
            "id": str(uuid.uuid4()),
            "tipo": "ACCIONES_RAPIDAS",
            "acciones": [{"id": action_id, "etiqueta": label} for action_id, label in QUICK_ACTIONS],
        }
        return text, [block]

    async def _persist_reply(
        self, conversation_id: uuid.UUID, text: str, blocks: list[dict[str, Any]], turn: "_Turn", started: float
    ) -> "_Persisted":
        latency_ms = int((time.monotonic() - started) * 1000)
        message = await self._messages.add(
            conversation_id,
            MessageRole.ASSISTANT,
            text,
            blocks={"blocks": blocks} if blocks else None,
            input_tokens=turn.input_tokens or None,
            output_tokens=turn.output_tokens or None,
            latency_ms=latency_ms,
        )
        await self._conversations.touch(conversation_id)
        await self._uow.commit()
        return _Persisted(message, latency_ms)


class _Turn:
    """Mutable bookkeeping of one assistant turn."""

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.blocks: list[dict[str, Any]] = []
        self.tool_names: list[str] = []
        self.validation_failures = 0
        self.input_tokens = 0
        self.output_tokens = 0


class _Persisted:
    def __init__(self, message: Message, latency_ms: int):
        self.message = message
        self.latency_ms = latency_ms


def _serialize(result: ToolResult) -> str:
    payload: dict[str, Any] = {"code": result.code, "data": dict(result.data)}
    if result.is_error:
        payload["error"] = True
    return json.dumps(payload, ensure_ascii=False, default=str)


def _wrap_tool_data(payload: str) -> str:
    """Delimit a tool result as data (SPEC-05 Req. 9) and keep it from closing the delimiter itself."""
    payload = payload.replace(TOOL_DATA_CLOSE, "").replace(TOOL_DATA_OPEN, "")
    if len(payload) > TOOL_RESULT_MAX_CHARS:
        payload = payload[:TOOL_RESULT_MAX_CHARS] + "...[truncado]"
    return f"{TOOL_DATA_OPEN}{payload}{TOOL_DATA_CLOSE}"
