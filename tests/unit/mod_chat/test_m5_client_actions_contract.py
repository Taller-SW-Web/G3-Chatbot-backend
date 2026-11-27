"""M5 task 4.4: client-only actions sent to the BFF as `accion` have no effect (SPEC-05 Req. Acciones de cliente del chat).

SPEC-05 · Scenario: El backend no borra cookies
SPEC-05 · Scenario: Pagar desde el chat (the backend creates no checkout)
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
from src.shared.application.ports.token_validator_port import InvalidTokenError
from src.shared.application.tools.tool_registry import ToolResult
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.domain.entities.conversation import Conversation
from src.shared.domain.entities.message import Message
from src.shared.infrastructure.http.dependencies.jwt_validator import get_access_token_validator

NOW = datetime(2026, 11, 26, tzinfo=timezone.utc)
SUBJECT = uuid.uuid4()
CID = uuid.uuid4()
URL = f"/api/v1/chat/conversaciones/{CID}/mensajes"
AUTH = {"Authorization": "Bearer good-token"}
SESSION_COOKIES = {"chat_rt": "refresh-token", "chat_chg": "challenge-token"}


class StubValidator:
    async def validate_customer(self, token):
        if token != "good-token":
            raise InvalidTokenError("bad token")
        return {"sub": str(SUBJECT)}


class FakeConversations:
    async def find_owned(self, conversation_id, owner):
        if conversation_id != CID or owner != CallerIdentity(customer_id=SUBJECT):
            return None
        return Conversation(CID, "Compra", NOW)

    async def touch(self, conversation_id):
        pass


class FakeMessages:
    def __init__(self):
        self.rows = []

    async def add(self, conversation_id, role, text, **extra):
        self.rows.append({"role": role, "text": text, **extra})
        return Message(uuid.uuid4(), conversation_id, role, text, extra.get("blocks"), NOW)


class FakeUow:
    async def commit(self):
        pass

    async def rollback(self):
        pass


@asynccontextmanager
async def no_turn():
    raise AssertionError("a direct action must not open an LLM turn")
    yield  # pragma: no cover


@pytest.fixture
def handlers():
    """Real registry with the tools that produce the client actions; any call is a side effect."""
    return {
        "cerrar_sesion": AsyncMock(return_value=ToolResult("OK")),
        "iniciar_checkout": AsyncMock(return_value=ToolResult("OK")),
        "nueva_conversacion": AsyncMock(return_value=ToolResult("OK")),
        "buscar_conversaciones": AsyncMock(return_value=ToolResult("OK")),
    }


@pytest.fixture
def client(handlers):
    messages = FakeMessages()

    @asynccontextmanager
    async def action_scope():
        yield DispatchDirectActionUseCase(
            FakeConversations(), messages, FakeUow(),
            ActionDispatcher(build_tool_registry(handlers)),
        )

    app = create_app(turn_scope=no_turn, action_scope=action_scope)
    app.dependency_overrides[get_access_token_validator] = StubValidator
    test_client = TestClient(app)
    test_client.cookies.update(SESSION_COOKIES)
    test_client.messages = messages
    return test_client


@pytest.mark.parametrize("action", ["CERRAR_SESION", "IR_A_CHECKOUT", "NUEVO_CHAT", "BUSCAR_CHATS"])
def test_client_action_sent_to_the_bff_is_unknown_and_has_no_effect(client, handlers, action):
    res = client.post(URL, headers=AUTH, json={"accion": {"tipo": action, "payload": {}}})

    assert res.status_code == 200
    assert res.json()["code"] == "ACCION_DESCONOCIDA"
    assert all(handler.await_count == 0 for handler in handlers.values())
    assert client.messages.rows[0]["result_code"] == "ACCION_DESCONOCIDA"


def test_cerrar_sesion_does_not_touch_the_session_cookies(client, handlers):
    res = client.post(URL, headers=AUTH, json={"accion": {"tipo": "CERRAR_SESION", "payload": {}}})

    assert res.json()["code"] == "ACCION_DESCONOCIDA"
    assert "set-cookie" not in res.headers
    assert {name: client.cookies.get(name) for name in SESSION_COOKIES} == SESSION_COOKIES
    handlers["cerrar_sesion"].assert_not_awaited()


def test_ir_a_checkout_does_not_create_a_checkout(client, handlers):
    res = client.post(
        URL, headers=AUTH, json={"accion": {"tipo": "IR_A_CHECKOUT", "payload": {"confirmado": True}}},
    )

    assert res.json()["code"] == "ACCION_DESCONOCIDA"
    handlers["iniciar_checkout"].assert_not_awaited()
