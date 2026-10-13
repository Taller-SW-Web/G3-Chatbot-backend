"""H3-10: OpenAI adapter of LLMProvider against a mocked HTTP layer. No network, no real key."""
import json

import httpx
import pytest
import respx

from src.adapters.outbound.nlp.openai_nlp_adapter import OpenAILLMProvider
from src.infrastructure.config.container import provide_llm_provider
from src.infrastructure.config.settings import Settings
from src.ports.outbound.llm_provider_port import (
    Completed,
    ImagePart,
    LLMMessage,
    LLMRole,
    LLMTimeoutError,
    LLMUnavailableError,
    TextDelta,
    TextPart,
    TokenUsage,
    ToolCall,
    ToolCallRequested,
    ToolDefinition,
)
from tests.fakes.fake_llm_provider import FakeLLMProvider

URL = "https://api.openai.com/v1/chat/completions"
pytestmark = pytest.mark.asyncio(loop_scope="function")
USER_HELLO = [LLMMessage(LLMRole.USER, (TextPart("hello"),))]


def make_provider(**overrides) -> OpenAILLMProvider:
    params = dict(api_key="test-key", model="test-model", temperature=0.6, max_tokens=256,
                  timeout_seconds=5.0)
    return OpenAILLMProvider(**{**params, **overrides})


def chunk(delta=None, finish_reason=None, usage=None) -> dict:
    return {
        "id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "test-model",
        "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        "usage": usage,
    }


def sse(*chunks: dict) -> httpx.Response:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


async def collect(provider, messages=USER_HELLO, tools=()):
    return [event async for event in provider.stream(messages, tools)]


@respx.mock
@pytest.mark.asyncio
async def test_streams_text_deltas_and_usage():
    route = respx.post(URL).mock(return_value=sse(
        chunk({"role": "assistant", "content": ""}),
        chunk({"content": "Hola"}),
        chunk({"content": " mundo"}),
        chunk({}, finish_reason="stop"),
        chunk(usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}),
    ))
    events = await collect(make_provider())
    assert events == [TextDelta("Hola"), TextDelta(" mundo"), Completed("stop", TokenUsage(7, 3))]
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "test-model"
    assert body["temperature"] == 0.6
    assert body["max_completion_tokens"] == 256
    assert body["stream"] is True
    assert "tools" not in body


@respx.mock
@pytest.mark.asyncio
async def test_assembles_tool_call_from_fragments():
    route = respx.post(URL).mock(return_value=sse(
        chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                               "function": {"name": "search_products", "arguments": ""}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"query": "zapa'}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'tillas", "size": 42}'}}]}),
        chunk({"tool_calls": [{"index": 1, "id": "call_2", "type": "function",
                               "function": {"name": "get_cart", "arguments": "{}"}}]}),
        chunk({}, finish_reason="tool_calls"),
    ))
    tool = ToolDefinition("search_products", "Search the catalog", {"type": "object", "properties": {}})
    events = await collect(make_provider(), tools=[tool])
    assert events == [
        ToolCallRequested(ToolCall("call_1", "search_products", {"query": "zapatillas", "size": 42})),
        ToolCallRequested(ToolCall("call_2", "get_cart", {})),
        Completed("tool_calls", None),
    ]
    body = json.loads(route.calls[0].request.content)
    assert body["tools"] == [{"type": "function", "function": {
        "name": "search_products", "description": "Search the catalog",
        "parameters": {"type": "object", "properties": {}}}}]


@respx.mock
@pytest.mark.asyncio
async def test_malformed_tool_arguments_raise_unavailable():
    respx.post(URL).mock(return_value=sse(
        chunk({"tool_calls": [{"index": 0, "id": "c", "type": "function",
                               "function": {"name": "t", "arguments": '{"a": '}}]}),
        chunk({}, finish_reason="tool_calls"),
    ))
    with pytest.raises(LLMUnavailableError):
        await collect(make_provider())


@respx.mock
@pytest.mark.asyncio
async def test_maps_history_images_and_tool_results_to_openai_messages():
    route = respx.post(URL).mock(return_value=sse(chunk({}, finish_reason="stop")))
    history = [
        LLMMessage(LLMRole.SYSTEM, (TextPart("be brief"),)),
        LLMMessage(LLMRole.USER, (TextPart("what is this?"), ImagePart("image/png", "QUJD"))),
        LLMMessage(LLMRole.ASSISTANT, tool_calls=(ToolCall("call_1", "search_products", {"q": "x"}),)),
        LLMMessage(LLMRole.TOOL, (TextPart('{"items": []}'),), tool_call_id="call_1"),
    ]
    await collect(make_provider(), history)
    assert json.loads(route.calls[0].request.content)["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        ]},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "search_products", "arguments": '{"q": "x"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"items": []}'},
    ]


@pytest.mark.asyncio
async def test_image_with_vision_disabled_is_rejected_without_calling_the_provider():
    provider = make_provider(vision_enabled=False)
    assert provider.supports_vision is False
    messages = [LLMMessage(LLMRole.USER, (ImagePart("image/png", "QUJD"),))]
    with pytest.raises(LLMUnavailableError):
        await collect(provider, messages)


@respx.mock
@pytest.mark.asyncio
async def test_timeout_raises_neutral_timeout_error():
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(LLMTimeoutError):
        await collect(make_provider())


@respx.mock
@pytest.mark.parametrize("status", [401, 429, 500, 503])
@pytest.mark.asyncio
async def test_http_errors_raise_neutral_unavailable_error(status):
    respx.post(URL).mock(return_value=httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(LLMUnavailableError) as error:
        await collect(make_provider())
    assert str(status) in str(error.value)
    assert "test-key" not in str(error.value)


@respx.mock
@pytest.mark.asyncio
async def test_connection_failure_raises_unavailable_error():
    respx.post(URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(LLMUnavailableError):
        await collect(make_provider())


@respx.mock
@pytest.mark.asyncio
async def test_stream_without_finish_reason_raises_unavailable_error():
    respx.post(URL).mock(return_value=sse(chunk({"content": "cut"})))
    with pytest.raises(LLMUnavailableError):
        await collect(make_provider())


@respx.mock
@pytest.mark.asyncio
async def test_missing_api_key_fails_lazily_without_calling_the_network():
    route = respx.post(URL)
    provider = make_provider(api_key=None)  # building it must not raise
    with pytest.raises(LLMUnavailableError):
        await collect(provider)
    assert not route.called


@pytest.mark.parametrize("temperature", [-0.1, 2.1])
def test_temperature_outside_provider_range_is_rejected(temperature):
    with pytest.raises(ValueError):
        make_provider(temperature=temperature)


def test_factory_builds_openai_without_api_key():
    assert isinstance(provide_llm_provider(Settings()), OpenAILLMProvider)


@pytest.mark.parametrize("name, error", [("claude", NotImplementedError), ("gemini", ValueError)])
def test_factory_rejects_unavailable_providers(name, error):
    with pytest.raises(error):
        provide_llm_provider(Settings(llm_provider=name))


@pytest.mark.asyncio
async def test_fake_provider_replays_scripted_turns():
    fake = FakeLLMProvider([[TextDelta("hi"), Completed("stop")], LLMTimeoutError("t")])
    assert await collect(fake) == [TextDelta("hi"), Completed("stop")]
    with pytest.raises(LLMTimeoutError):
        await collect(fake)
    assert len(fake.calls) == 2
