"""Unit tests for OTP redaction in mod-chat (SPEC-05, ADR-0006, #252)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.modules.mod_chat.application.interpret_and_respond_use_case import (
    InterpretAndRespondUseCase,
)
from src.modules.mod_chat.module import sanitize_sensitive_text
from src.modules.mod_chat.ports.llm_provider_port import Completed, LLMMessage, TextDelta
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.domain.entities.conversation import Conversation
from src.shared.domain.entities.message import Message, MessageRole


class _FakeLLM:
    supports_vision = False

    def __init__(self) -> None:
        self.received_messages: list[LLMMessage] = []

    async def stream(self, messages, tools):
        self.received_messages = list(messages)
        yield TextDelta("Ingresa el código en el formulario seguro.")
        yield Completed(finish_reason="stop")


class _FakeConversations:
    def __init__(self, conv: Conversation, context: dict | None = None) -> None:
        self._conv = conv
        self._context = dict(context or {})

    async def find_owned(self, conversation_id, caller):
        return self._conv if conversation_id == self._conv.id else None

    async def get_context(self, conversation_id):
        return dict(self._context)

    async def save_context(self, conversation_id, context):
        self._context = dict(context)

    async def assign_title_if_missing(self, conversation_id, title):
        return True

    async def touch(self, conversation_id):
        return None


class _FakeMessages:
    def __init__(self, initial: list[Message] | None = None) -> None:
        self.items: list[Message] = list(initial or [])

    async def add(self, conversation_id, role, text, **kwargs):
        msg = Message(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            role=role,
            text=text,
            blocks=tuple(kwargs.get("blocks") or ()),
            created_at=datetime.now(timezone.utc),
        )
        self.items.append(msg)
        return msg

    async def list_dialogue(self, conversation_id, limit=12):
        return list(self.items[-limit:])


def _build_use_case(
    *,
    context: dict | None = None,
    initial_messages: list[Message] | None = None,
):
    conv_id = uuid.uuid4()
    conv = Conversation(
        id=conv_id,
        title="Verificación",
        last_message_at=datetime.now(timezone.utc),
        summary=None,
    )
    llm = _FakeLLM()
    messages = _FakeMessages(initial_messages)
    uow = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    tools = SimpleNamespace(definitions=lambda: [], execute=AsyncMock())
    uc = InterpretAndRespondUseCase(
        conversations=_FakeConversations(conv, context=context),
        messages=messages,
        uow=uow,
        llm=llm,
        tools=tools,
        assistant_name="Botleta",
        llm_timeout_seconds=15.0,
        sanitize_text=sanitize_sensitive_text,
    )
    return uc, conv_id, messages, llm


@pytest.mark.asyncio
async def test_redacts_otp_when_conversation_context_has_pending_otp() -> None:
    uc, conv_id, messages, llm = _build_use_case(context={"otpPendiente": True})
    caller = CallerIdentity(customer_id=uuid.uuid4())

    stream = await uc.send_message(caller, conv_id, "Mi código es 482910")
    async for _ in stream:
        pass

    customer_msgs = [m for m in messages.items if m.role == MessageRole.CUSTOMER]
    assert len(customer_msgs) == 1
    assert customer_msgs[0].text == "Mi código es [OTP oculto]"
    assert "482910" not in customer_msgs[0].text

    llm_payload = str(llm.received_messages)
    assert "482910" not in llm_payload
    assert "[OTP oculto]" in llm_payload


@pytest.mark.asyncio
async def test_redacts_otp_when_previous_assistant_message_opened_otp_form() -> None:
    conv_id = uuid.uuid4()
    prior_assistant = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        role=MessageRole.ASSISTANT,
        text="Confirma tu celular con el código.",
        blocks=({"tipo": "FORMULARIO", "formulario": "OTP_CELULAR"},),
        created_at=datetime.now(timezone.utc),
    )
    uc, _, messages, llm = _build_use_case(initial_messages=[prior_assistant])
    # Align conversation_id with the prior message
    conv_id = uc._conversations._conv.id  # type: ignore[attr-defined]
    caller = CallerIdentity(customer_id=uuid.uuid4())

    stream = await uc.send_message(caller, conv_id, "739201")
    async for _ in stream:
        pass

    customer_msgs = [m for m in messages.items if m.role == MessageRole.CUSTOMER]
    assert customer_msgs[0].text == "[OTP oculto]"
    assert "739201" not in str(llm.received_messages)


@pytest.mark.asyncio
async def test_does_not_redact_six_digits_when_no_otp_is_pending() -> None:
    uc, conv_id, messages, llm = _build_use_case(context={})
    caller = CallerIdentity(customer_id=uuid.uuid4())

    stream = await uc.send_message(caller, conv_id, "Busco el modelo 482910")
    async for _ in stream:
        pass

    customer_msgs = [m for m in messages.items if m.role == MessageRole.CUSTOMER]
    assert customer_msgs[0].text == "Busco el modelo 482910"
    assert "482910" in str(llm.received_messages)
