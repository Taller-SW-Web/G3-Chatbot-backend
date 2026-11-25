"""Scripted LLMProvider for load tests (`LLM_PROVIDER=fake`, H5-P2). No network."""
import pytest

from src.modules.mod_chat.adapters.scripted_llm_provider import ScriptedLLMProvider
from src.modules.mod_chat.module import provide_llm_provider
from src.modules.mod_chat.ports.llm_provider_port import (
    Completed,
    LLMMessage,
    LLMRole,
    TextDelta,
    TextPart,
    ToolCallRequested,
    ToolDefinition,
)
from src.shared.infrastructure.config.settings import Settings

SEARCH = ToolDefinition("buscar_productos", "Buscar productos", {"type": "object"})


async def collect(provider, messages, tools):
    return [event async for event in provider.stream(messages, tools)]


@pytest.mark.asyncio
async def test_user_message_requests_the_search_tool_with_the_customer_text():
    events = await collect(
        ScriptedLLMProvider(),
        [LLMMessage(LLMRole.SYSTEM, (TextPart("sys"),)), LLMMessage(LLMRole.USER, (TextPart(" zapatillas running "),))],
        [SEARCH],
    )

    call = events[0]
    assert isinstance(call, ToolCallRequested)
    assert call.call.name == "buscar_productos"
    assert call.call.arguments == {"q": "zapatillas running"}
    assert events[-1] == Completed("tool_calls", events[-1].usage)


@pytest.mark.asyncio
async def test_tool_result_is_answered_with_text_and_no_new_call():
    events = await collect(
        ScriptedLLMProvider(),
        [LLMMessage(LLMRole.USER, (TextPart("hola"),)), LLMMessage(LLMRole.TOOL, (TextPart("{}"),), tool_call_id="c1")],
        [SEARCH],
    )

    assert all(not isinstance(event, ToolCallRequested) for event in events)
    assert "".join(e.text for e in events if isinstance(e, TextDelta))
    assert events[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_without_the_search_tool_it_only_answers_text():
    events = await collect(ScriptedLLMProvider(), [LLMMessage(LLMRole.USER, (TextPart("hola"),))], [])

    assert any(isinstance(event, TextDelta) for event in events)
    assert events[-1].finish_reason == "stop"


def test_factory_builds_the_scripted_provider_outside_production():
    provider = provide_llm_provider(Settings(llm_provider="fake"))

    assert isinstance(provider, ScriptedLLMProvider)
    assert provider.supports_vision is False


def test_factory_refuses_the_scripted_provider_in_production():
    settings = Settings(llm_provider="fake", app_env="production", allow_fake_security=True)

    with pytest.raises(ValueError, match="not allowed"):
        provide_llm_provider(settings)
