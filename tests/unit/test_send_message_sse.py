"""POST /chat/conversaciones/{id}/mensajes streamed as Server-Sent Events (SPEC-05 Req. 4)."""
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from src.adapters.inbound.http.dependencies.caller_identity import hash_chat_sid
from src.application.dto.conversation_events import (
    BlockEvent,
    DegradedEvent,
    FinalEvent,
    MessageAcceptedEvent,
    TokenEvent,
)
from src.application.use_cases.interpret_and_respond_use_case import InvalidMessageError
from src.domain.entities.message import Message, MessageRole
from src.domain.value_objects.caller_identity import CallerIdentity
from src.infrastructure.main import create_app
from src.ports.inbound.chatbot_service_port import ConversationNotFoundError

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
CID = uuid.uuid4()
SID = "raw-cookie-value"
OWNER = CallerIdentity(anonymous_sid=hash_chat_sid(SID))
CUSTOMER_ID, ASSISTANT_ID = uuid.uuid4(), uuid.uuid4()
URL = f"/api/v1/chat/conversaciones/{CID}/mensajes"


def message(role, text, message_id):
    return Message(id=message_id, conversation_id=CID, role=role, text=text, blocks=None, created_at=NOW)


ACCEPTED = MessageAcceptedEvent(message(MessageRole.CUSTOMER, "hola", CUSTOMER_ID))
FINAL = FinalEvent(message(MessageRole.ASSISTANT, "Hola, ¿en qué te ayudo?", ASSISTANT_ID))
PLAIN_TURN = [ACCEPTED, TokenEvent("Hola, "), TokenEvent("¿en qué te ayudo?"), FINAL]


class FakeTurnService:
    """Scripted stand-in for InterpretAndRespondUseCase; records scope usage and stream closing."""

    def __init__(self, events=PLAIN_TURN):
        self.events = list(events)
        self.sent: list[tuple[CallerIdentity | None, uuid.UUID, str]] = []
        self.opened = self.closed = 0
        self.streams_started = self.streams_closed = 0
        self.stream_gate: asyncio.Event | None = None  # set to make the stream wait after the first event

    @asynccontextmanager
    async def scope(self):
        self.opened += 1
        try:
            yield self
        finally:
            self.closed += 1

    async def send_message(self, caller, conversation_id, text):
        if not text.strip() or len(text) > 1000:
            raise InvalidMessageError("message must have 1 to 1000 characters")
        if caller != OWNER or conversation_id != CID:
            raise ConversationNotFoundError(conversation_id)
        self.sent.append((caller, conversation_id, text))
        return self._stream()

    async def _stream(self):
        self.streams_started += 1
        try:
            for index, event in enumerate(self.events):
                if isinstance(event, Exception):
                    raise event
                yield event
                if index == 0 and self.stream_gate is not None:
                    await self.stream_gate.wait()
        finally:
            self.streams_closed += 1


def make_client(service, *, cookie=SID, action_scope=None):
    client = TestClient(create_app(turn_scope=service.scope, action_scope=action_scope))
    if cookie:
        client.cookies.set("chat_sid", cookie)
    return client


class FakeDirectActionService:
    def __init__(self):
        self.actions = []

    @asynccontextmanager
    async def scope(self):
        yield self

    async def execute(self, caller, conversation_id, action):
        self.actions.append((caller, conversation_id, action))
        from src.application.use_cases.dispatch_direct_action_use_case import DirectActionResult

        assistant = message(MessageRole.ASSISTANT, "", ASSISTANT_ID)
        return DirectActionResult(
            "OK", assistant, "", ({"tipo": "FORMULARIO", "formulario": "REGISTRO"},), {},
        )


def parse_sse(body: str) -> list[tuple[str, dict]]:
    frames = []
    for chunk in body.split("\n\n"):
        if not chunk:
            continue
        lines = chunk.split("\n")
        assert lines[0].startswith("event: ") and lines[1].startswith("data: ") and len(lines) == 2
        frames.append((lines[0][7:], json.loads(lines[1][6:])))
    return frames


def test_full_turn_is_streamed_as_sse_frames_ending_with_fin():
    service = FakeTurnService()
    res = make_client(service).post(URL, json={"texto": "hola"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert res.headers["cache-control"] == "no-cache"
    assert res.headers["x-accel-buffering"] == "no"
    assert parse_sse(res.content.decode("utf-8")) == [
        ("aceptado", {"mensajeId": str(CUSTOMER_ID)}),
        ("token", {"texto": "Hola, "}),
        ("token", {"texto": "¿en qué te ayudo?"}),
        ("fin", {"mensajeId": str(ASSISTANT_ID)}),
    ]
    assert "¿en qué" in res.content.decode("utf-8")  # ensure_ascii=False
    assert service.sent == [(OWNER, CID, "hola")]
    assert service.streams_closed == 1 and service.opened == service.closed == 1


def test_direct_ui_action_returns_rest_result_without_starting_sse_or_llm():
    turn_service = FakeTurnService()
    action_service = FakeDirectActionService()
    res = make_client(turn_service, action_scope=action_service.scope).post(
        URL,
        json={"accion": {"tipo": "SOLICITAR_REGISTRO", "payload": {}}},
    )

    assert res.status_code == 200
    assert res.json() == {
        "code": "OK",
        "mensajeId": str(ASSISTANT_ID),
        "texto": "",
        "bloques": [{"tipo": "FORMULARIO", "formulario": "REGISTRO"}],
        "datos": {},
    }
    assert action_service.actions == [
        (OWNER, CID, {"tipo": "SOLICITAR_REGISTRO", "payload": {}}),
    ]
    assert turn_service.sent == [] and turn_service.opened == 0


def test_direct_action_cannot_be_combined_with_chat_text():
    res = make_client(FakeTurnService()).post(
        URL,
        json={"texto": "hola", "accion": {"tipo": "SOLICITAR_REGISTRO", "payload": {}}},
    )

    assert res.status_code == 400
    assert res.json()["code"] == "VALIDACION"


def test_a_turn_with_a_tool_block_streams_the_block_before_fin():
    block = {"id": "b1", "tipo": "CARRUSEL_PRODUCTOS", "productos": []}
    service = FakeTurnService([ACCEPTED, BlockEvent(block), TokenEvent("Encontré uno"), FINAL])
    frames = parse_sse(make_client(service).post(URL, json={"texto": "zapatillas"}).text)
    assert [name for name, _ in frames] == ["aceptado", "bloque", "token", "fin"]
    assert frames[1][1] == {"bloque": block}


def test_degraded_turn_tells_the_client_to_discard_earlier_tokens():
    quick = {"id": "q", "tipo": "ACCIONES_RAPIDAS", "acciones": []}
    service = FakeTurnService(
        [ACCEPTED, TokenEvent("parc"), DegradedEvent("timeout"), BlockEvent(quick), FINAL]
    )
    frames = parse_sse(make_client(service).post(URL, json={"texto": "hola"}).text)
    assert [name for name, _ in frames] == ["aceptado", "token", "degradado", "bloque", "fin"]
    assert frames[2][1] == {"motivo": "timeout"}


@pytest.mark.parametrize("body", [{"texto": ""}, {"texto": "   "}, {"texto": "x" * 1001}, {}])
def test_blank_or_too_long_text_is_a_400_before_any_stream(body):
    service = FakeTurnService()
    res = make_client(service).post(URL, json=body)
    assert res.status_code == 400
    assert res.headers["content-type"].startswith("application/problem+json")
    assert res.json()["code"] == "VALIDACION"
    assert service.sent == [] and service.opened == service.closed == 1


def test_malformed_body_is_a_400_validation_error():
    res = make_client(FakeTurnService()).post(URL, json={"texto": 5})
    assert res.status_code == 400 and res.json()["code"] == "VALIDACION"
    res = make_client(FakeTurnService()).post(URL, json={"texto": "hola", "otro": 1})
    assert res.status_code == 400 and res.json()["code"] == "VALIDACION"


def test_foreign_or_missing_conversation_is_a_404():
    service = FakeTurnService()
    url = f"/api/v1/chat/conversaciones/{uuid.uuid4()}/mensajes"
    res = make_client(service).post(url, json={"texto": "hola"})
    assert res.status_code == 404 and res.json()["code"] == "RECURSO_NO_ENCONTRADO"
    assert service.opened == service.closed == 1


def test_someone_elses_cookie_and_missing_identity_are_404():
    for cookie in ("another-visitor", None):
        res = make_client(FakeTurnService(), cookie=cookie).post(URL, json={"texto": "hola"})
        assert res.status_code == 404 and res.json()["code"] == "RECURSO_NO_ENCONTRADO"


def test_authorization_header_is_501_before_anything_runs():
    service = FakeTurnService()
    res = make_client(service).post(URL, json={"texto": "hola"}, headers={"Authorization": "Bearer abc"})
    assert res.status_code == 501 and res.json()["code"] == "AUTENTICACION_NO_DISPONIBLE"
    assert service.opened == 0


@pytest.mark.parametrize(
    "body",
    [
        {"accion": {"tipo": "VER_OFERTAS", "payload": {}}},
        {"texto": "hola", "adjuntoIds": ["a1"]},
    ],
)
def test_actions_and_attachments_are_501_without_touching_the_use_case(body):
    service = FakeTurnService()
    res = make_client(service).post(URL, json=body)
    assert res.status_code == 501 and res.json()["code"] == "FUNCIONALIDAD_NO_DISPONIBLE"
    assert service.opened == 0


def test_unexpected_failure_mid_stream_ends_with_a_generic_error_frame():
    service = FakeTurnService([ACCEPTED, TokenEvent("a"), RuntimeError("db password=hunter2")])
    res = make_client(service).post(URL, json={"texto": "hola"})
    assert res.status_code == 200
    frames = parse_sse(res.text)
    assert [name for name, _ in frames] == ["aceptado", "token", "error"]
    assert frames[-1][1] == {"code": "ERROR_INTERNO"}
    assert "hunter2" not in res.text
    assert service.streams_closed == 1 and service.opened == service.closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("asgi_spec", ["2.3", "2.4"])
async def test_client_disconnect_mid_stream_closes_the_iterator_and_releases_the_scope(asgi_spec):
    service = FakeTurnService()
    service.stream_gate = asyncio.Event()  # the turn stalls after `aceptado`, like a slow LLM
    asyncio.get_running_loop().call_later(0.2, service.stream_gate.set)
    app = create_app(turn_scope=service.scope)
    body = b'{"texto": "hola"}'
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": asgi_spec}, "http_version": "1.1",
        "method": "POST", "path": URL, "raw_path": URL.encode(), "query_string": b"", "root_path": "",
        "scheme": "http", "server": ("test", 80), "client": ("test", 1),
        "headers": [
            (b"host", b"test"), (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()), (b"cookie", f"chat_sid={SID}".encode()),
        ],
    }
    sent_body = False
    first_chunk = asyncio.Event()
    chunks: list[bytes] = []

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        await first_chunk.wait()
        if asgi_spec == "2.4":
            await asyncio.Event().wait()  # ASGI 2.4 servers report a disconnect through `send`, not `receive`
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            if asgi_spec == "2.4" and chunks:
                raise OSError("client disconnected")
            chunks.append(message["body"])
            first_chunk.set()
        await asyncio.sleep(0)  # a real server awaits I/O here

    running = asyncio.wait_for(app(scope, receive, send), timeout=5)
    if asgi_spec == "2.4":
        with pytest.raises(ClientDisconnect):  # Starlette's signal for a failed send; servers swallow it
            await running
    else:
        await running

    # The disconnect landed while the use case was mid-step: the step finishes (shielded), then the turn stops.
    assert b"event: aceptado" in chunks[0] and not any(b"event: fin" in c for c in chunks)
    assert service.streams_started == service.streams_closed == 1
    assert service.opened == service.closed == 1
