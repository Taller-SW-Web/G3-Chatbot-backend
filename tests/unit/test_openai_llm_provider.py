"""OpenAI adapter of LLMProvider against a mocked HTTP layer. No network, no real key."""
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

URL = "https://api.openai.com/v1/responses"
pytestmark = pytest.mark.asyncio(loop_scope="function")
USER_HELLO = [LLMMessage(LLMRole.USER, (TextPart("hello"),))]
SEARCH_TOOL = ToolDefinition("search_products", "Search the catalog", {"type": "object", "properties": {}})


def make_provider(**overrides) -> OpenAILLMProvider:
    params = dict(api_key="test-key", model="test-model", temperature=0.6, max_tokens=256,
                  timeout_seconds=5.0)
    return OpenAILLMProvider(**{**params, **overrides})


def final_response(status="completed", usage=None, incomplete_reason=None) -> dict:
    return {
        "id": "resp_1", "object": "response", "created_at": 0, "model": "test-model",
        "status": status, "output": [], "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "usage": usage,
        "incomplete_details": {"reason": incomplete_reason} if incomplete_reason else None,
    }


def usage_of(input_tokens=7, output_tokens=3) -> dict:
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}


def text_delta(text: str) -> dict:
    return {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0,
            "content_index": 0, "delta": text, "logprobs": []}


def call_item(index: int, call_id: str, name: str, arguments: str, status="in_progress") -> dict:
    return {"type": "function_call", "id": f"fc_{index}", "call_id": call_id, "name": name,
            "arguments": arguments, "status": status}


def call_added(index: int, call_id: str, name: str) -> dict:
    return {"type": "response.output_item.added", "output_index": index,
            "item": call_item(index, call_id, name, "")}


def args_delta(index: int, fragment: str) -> dict:
    return {"type": "response.function_call_arguments.delta", "item_id": f"fc_{index}",
            "output_index": index, "delta": fragment}


def call_done(index: int, call_id: str, name: str, arguments: str) -> dict:
    return {"type": "response.output_item.done", "output_index": index,
            "item": call_item(index, call_id, name, arguments, "completed")}


def completed(usage=None) -> dict:
    return {"type": "response.completed", "response": final_response(usage=usage)}


def sse(*events: dict) -> httpx.Response:
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


async def collect(provider, messages=USER_HELLO, tools=()):
    return [event async for event in provider.stream(messages, tools)]


@respx.mock
@pytest.mark.asyncio
async def test_streams_text_deltas_and_usage_with_default_request_shape():
    route = respx.post(URL).mock(return_value=sse(
        text_delta("Hola"), text_delta(" mundo"), completed(usage_of(7, 3)),
    ))
    events = await collect(make_provider())
    assert events == [TextDelta("Hola"), TextDelta(" mundo"), Completed("stop", TokenUsage(7, 3))]
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "test-model"
    assert body["temperature"] == 0.6
    assert body["max_output_tokens"] == 256
    assert body["reasoning"] == {"effort": "none"}
    assert body["store"] is False
    assert "previous_response_id" not in body
    assert body["stream"] is True
    assert "tools" not in body


@respx.mock
@pytest.mark.asyncio
async def test_assembles_tool_calls_from_argument_deltas():
    route = respx.post(URL).mock(return_value=sse(
        call_added(0, "call_1", "search_products"),
        args_delta(0, '{"query": "zapa'),
        args_delta(0, 'tillas", "size": 42}'),
        call_done(0, "call_1", "search_products", '{"query": "zapatillas", "size": 42}'),
        call_added(1, "call_2", "get_cart"),
        call_done(1, "call_2", "get_cart", "{}"),
        completed(),
    ))
    events = await collect(make_provider(), tools=[SEARCH_TOOL])
    assert events == [
        ToolCallRequested(ToolCall("call_1", "search_products", {"query": "zapatillas", "size": 42})),
        ToolCallRequested(ToolCall("call_2", "get_cart", {})),
        Completed("tool_calls", None),
    ]
    body = json.loads(route.calls[0].request.content)
    assert body["tools"] == [{
        "type": "function", "name": "search_products", "description": "Search the catalog",
        "parameters": {"type": "object", "properties": {}}, "strict": False}]


@respx.mock
@pytest.mark.asyncio
async def test_tool_call_arguments_fall_back_to_deltas_when_item_done_is_missing():
    respx.post(URL).mock(return_value=sse(
        call_added(0, "call_1", "get_cart"), args_delta(0, '{"a":'), args_delta(0, " 1}"), completed(),
    ))
    events = await collect(make_provider())
    assert events[0] == ToolCallRequested(ToolCall("call_1", "get_cart", {"a": 1}))


@respx.mock
@pytest.mark.asyncio
async def test_mixed_text_then_tool_call():
    respx.post(URL).mock(return_value=sse(
        text_delta("Let me check."),
        call_added(1, "call_1", "get_cart"),
        call_done(1, "call_1", "get_cart", "{}"),
        completed(usage_of(4, 2)),
    ))
    assert await collect(make_provider()) == [
        TextDelta("Let me check."),
        ToolCallRequested(ToolCall("call_1", "get_cart", {})),
        Completed("tool_calls", TokenUsage(4, 2)),
    ]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ['{"a": ', "[1, 2]"])
async def test_malformed_or_non_object_tool_arguments_raise_unavailable(arguments):
    respx.post(URL).mock(return_value=sse(
        call_added(0, "c", "t"), call_done(0, "c", "t", arguments), completed(),
    ))
    with pytest.raises(LLMUnavailableError):
        await collect(make_provider())


@respx.mock
@pytest.mark.asyncio
async def test_maps_history_images_and_tool_results_to_response_input_items():
    route = respx.post(URL).mock(return_value=sse(completed()))
    history = [
        LLMMessage(LLMRole.SYSTEM, (TextPart("be brief"),)),
        LLMMessage(LLMRole.USER, (TextPart("what is this?"), ImagePart("image/png", "QUJD"))),
        LLMMessage(LLMRole.ASSISTANT, (TextPart("Checking."),),
                   tool_calls=(ToolCall("call_1", "search_products", {"q": "x"}),)),
        LLMMessage(LLMRole.TOOL, (TextPart('{"items": []}'),), tool_call_id="call_1"),
        LLMMessage(LLMRole.ASSISTANT, (TextPart("Nothing found."),)),
    ]
    await collect(make_provider(), history)
    assert json.loads(route.calls[0].request.content)["input"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [
            {"type": "input_text", "text": "what is this?"},
            {"type": "input_image", "image_url": "data:image/png;base64,QUJD", "detail": "auto"},
        ]},
        {"role": "assistant", "content": "Checking."},
        {"type": "function_call", "call_id": "call_1", "name": "search_products",
         "arguments": '{"q": "x"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": '{"items": []}'},
        {"role": "assistant", "content": "Nothing found."},
    ]


@respx.mock
@pytest.mark.asyncio
async def test_assistant_tool_call_without_text_emits_only_the_function_call():
    route = respx.post(URL).mock(return_value=sse(completed()))
    history = [LLMMessage(LLMRole.ASSISTANT, tool_calls=(ToolCall("call_1", "get_cart", {}),))]
    await collect(make_provider(), history)
    assert json.loads(route.calls[0].request.content)["input"] == [
        {"type": "function_call", "call_id": "call_1", "name": "get_cart", "arguments": "{}"}]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high"])
async def test_reasoning_effort_is_sent_and_temperature_omitted_unless_none(effort):
    route = respx.post(URL).mock(return_value=sse(completed()))
    await collect(make_provider(reasoning_effort=effort))
    body = json.loads(route.calls[0].request.content)
    assert body["reasoning"] == {"effort": effort}
    assert "temperature" not in body


def test_invalid_reasoning_effort_is_rejected():
    with pytest.raises(ValueError, match="LLM_REASONING_EFFORT"):
        make_provider(reasoning_effort="extreme")


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
@pytest.mark.parametrize("reason, finish", [("max_output_tokens", "length"), ("content_filter", "content_filter")])
async def test_incomplete_response_maps_to_finish_reason(reason, finish):
    respx.post(URL).mock(return_value=sse(
        text_delta("cut"),
        {"type": "response.incomplete",
         "response": final_response("incomplete", usage_of(5, 256), reason)},
    ))
    assert await collect(make_provider()) == [
        TextDelta("cut"), Completed(finish, TokenUsage(5, 256))]


@respx.mock
@pytest.mark.asyncio
async def test_failed_response_raises_unavailable_error():
    respx.post(URL).mock(return_value=sse(
        {"type": "response.failed", "response": final_response("failed")}))
    with pytest.raises(LLMUnavailableError):
        await collect(make_provider())


@respx.mock
@pytest.mark.asyncio
async def test_error_event_raises_unavailable_error():
    respx.post(URL).mock(return_value=sse(
        {"type": "error", "code": "server_error", "message": "boom", "param": None}))
    with pytest.raises(LLMUnavailableError):
        await collect(make_provider())


@respx.mock
@pytest.mark.asyncio
async def test_stream_without_final_response_raises_unavailable_error():
    respx.post(URL).mock(return_value=sse(text_delta("cut")))
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


def test_settings_reasoning_effort_defaults_to_none_and_validates(monkeypatch):
    from src.infrastructure.config.settings import get_settings

    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    get_settings.cache_clear()
    assert get_settings().llm_reasoning_effort == "none"
    monkeypatch.setenv("LLM_REASONING_EFFORT", "Low")
    get_settings.cache_clear()
    assert get_settings().llm_reasoning_effort == "low"
    monkeypatch.setenv("LLM_REASONING_EFFORT", "extreme")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="LLM_REASONING_EFFORT"):
        get_settings()
    get_settings.cache_clear()


def test_factory_passes_reasoning_effort_to_the_provider():
    provider = provide_llm_provider(Settings(llm_reasoning_effort="low"))
    assert provider._reasoning_effort == "low"
