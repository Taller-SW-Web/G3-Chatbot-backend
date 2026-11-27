"""M5 task 4.4: client actions sent back to the BFF are unknown and have no side effects.

The four client actions (`IR_A_CHECKOUT`, `CERRAR_SESION`, `NUEVO_CHAT`, `BUSCAR_CHATS`)
are executed by the frontend. If one reaches `POST /chat/conversaciones/{id}/mensajes`
as `accion`, the real dispatcher and registry must answer `ACCION_DESCONOCIDA`, no
handler may run and the backend must not touch the session cookies.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src.infrastructure.main import create_app
from src.modules.mod_catalog.application.tools import build_tool_registry
from src.modules.mod_chat.application.dispatch_direct_action_use_case import DispatchDirectActionUseCase
from src.modules.mod_chat.application.tools.action_dispatcher import ActionDispatcher
from src.shared.application.tools.tool_contracts import TOOL_CONTRACTS
from src.shared.domain.entities.conversation import Conversation
from src.shared.domain.entities.message import Message, MessageRole
from src.shared.infrastructure.http.dependencies.jwt_validator import get_access_token_validator

NOW = datetime(2026, 11, 25, tzinfo=timezone.utc)
CUSTOMER_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()
URL = f"/api/v1/chat/conversaciones/{CONVERSATION_ID}/mensajes"
AUTHORIZATION = {"Authorization": "Bearer good-token"}
CLIENT_ACTIONS = ("IR_A_CHECKOUT", "CERRAR_SESION", "NUEVO_CHAT", "BUSCAR_CHATS")


class AcceptingValidator:
    async def validate_customer(self, token):
        assert token == "good-token"
        return {"sub": str(CUSTOMER_ID)}


class FakeConversations:
    def __init__(self):
        self.touched = 0

    async def find_owned(self, conversation_id, owner):
        if conversation_id != CONVERSATION_ID or owner.customer_id != CUSTOMER_ID:
            return None
        return Conversation(CONVERSATION_ID, "Cuenta", NOW)

    async def touch(self, conversation_id):
        self.touched += 1


class FakeMessages:
    def __init__(self):
        self.rows = []

    async def add(self, conversation_id, role, text, **extra):
        self.rows.append({"role": role, "text": text, **extra})
        return Message(uuid.uuid4(), conversation_id, role, text, extra.get("blocks"), NOW)


class FakeUow:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class FakeTurnScope:
    """The direct-action path must not open a turn (its use case is not mounted)."""

    def __init__(self):
        self.opened = 0

    @asynccontextmanager
    async def scope(self):
        self.opened += 1
        yield None


def tool_handlers() -> dict[str, AsyncMock]:
    return {
        contract.name: AsyncMock()
        for contract in TOOL_CONTRACTS
        if contract.name != "solicitar_registro"
    }


def make_client(handlers):
    conversations, messages, uow = FakeConversations(), FakeMessages(), FakeUow()
    use_case = DispatchDirectActionUseCase(
        conversations,
        messages,
        uow,
        ActionDispatcher(build_tool_registry(handlers)),
        sanitize_text=lambda text: text,
    )

    @asynccontextmanager
    async def action_scope():
        yield use_case

    turn_scope = FakeTurnScope()
    app = create_app(turn_scope=turn_scope.scope, action_scope=action_scope)
    app.dependency_overrides[get_access_token_validator] = AcceptingValidator
    client = TestClient(app)
    client.cookies.set("chat_sid", "sid-cookie")
    client.cookies.set("chat_rt", "refresh-cookie")
    client.cookies.set("chat_chg", "challenge-cookie")
    return client, conversations, messages, uow, turn_scope


def post_action(client, action, payload=None):
    return client.post(
        URL,
        json={"accion": {"tipo": action, "payload": payload if payload is not None else {}}},
        headers=AUTHORIZATION,
    )


@pytest.mark.parametrize("action", CLIENT_ACTIONS)
def test_client_actions_are_unknown_over_http_and_run_no_handler(action):
    handlers = tool_handlers()
    client, conversations, messages, uow, turn_scope = make_client(handlers)

    res = post_action(client, action)

    assert res.status_code == 200
    body = res.json()
    assert body["code"] == "ACCION_DESCONOCIDA"
    assert body["texto"] == "" and body["datos"] == {}
    assert [block["code"] for block in body["bloques"]] == ["ACCION_DESCONOCIDA"]
    assert all(handler.await_count == 0 for handler in handlers.values())
    assert turn_scope.opened == 0
    # Only the audit + assistant rows are written; the action itself has no effect.
    assert [row["role"] for row in messages.rows] == [MessageRole.TOOL, MessageRole.ASSISTANT]
    assert conversations.touched == 1 and uow.commits == 1 and uow.rollbacks == 0


def test_cerrar_sesion_sends_no_set_cookie_and_keeps_the_session_cookies():
    handlers = tool_handlers()
    client, *_ = make_client(handlers)
    names = ("chat_sid", "chat_rt", "chat_chg")
    before = {name: client.cookies.get(name) for name in names}
    assert all(value is not None for value in before.values())

    res = post_action(client, "CERRAR_SESION")

    assert res.status_code == 200 and res.json()["code"] == "ACCION_DESCONOCIDA"
    request_cookies = res.request.headers["cookie"]
    assert "chat_rt=refresh-cookie" in request_cookies
    assert "chat_chg=challenge-cookie" in request_cookies
    assert "set-cookie" not in res.headers
    assert {name: client.cookies.get(name) for name in names} == before
    handlers["cerrar_sesion"].assert_not_awaited()


def test_ir_a_checkout_with_confirmado_true_never_runs_the_checkout_handler():
    handlers = tool_handlers()
    client, conversations, messages, uow, turn_scope = make_client(handlers)

    res = post_action(client, "IR_A_CHECKOUT", {"confirmado": True})

    assert res.status_code == 200 and res.json()["code"] == "ACCION_DESCONOCIDA"
    handlers["iniciar_checkout"].assert_not_awaited()
    assert all(handler.await_count == 0 for handler in handlers.values())
    assert turn_scope.opened == 0
    # No checkout exists: the only port that could create one is the handler above.
    assert [row["role"] for row in messages.rows] == [MessageRole.TOOL, MessageRole.ASSISTANT]
    assert conversations.touched == 1 and uow.commits == 1 and uow.rollbacks == 0
