import uuid
from datetime import datetime, timezone

import pytest

from src.application.tools.action_dispatcher import ActionDispatcher
from src.application.tools.catalog_tools import build_tool_registry
from src.application.use_cases.dispatch_direct_action_use_case import DispatchDirectActionUseCase
from src.domain.entities.conversation import Conversation
from src.domain.entities.message import Message, MessageRole
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.inbound.chatbot_service_port import ConversationNotFoundError

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


class FakeConversations:
    def __init__(self, owner):
        self.id = uuid.uuid4()
        self.owner = owner
        self.touched = 0

    async def find_owned(self, conversation_id, owner):
        if conversation_id != self.id or owner != self.owner:
            return None
        return Conversation(self.id, "Cuenta", NOW)

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


def make_use_case(owner):
    conversations, messages, uow = FakeConversations(owner), FakeMessages(), FakeUow()
    use_case = DispatchDirectActionUseCase(
        conversations,
        messages,
        uow,
        ActionDispatcher(build_tool_registry({})),
        sanitize_text=lambda text: text,
    )
    return use_case, conversations, messages, uow


@pytest.mark.asyncio
async def test_direct_action_uses_registry_and_persists_tool_and_assistant_history():
    caller = CallerIdentity(anonymous_sid="sid-hash")
    use_case, conversations, messages, uow = make_use_case(caller)

    result = await use_case.execute(
        caller,
        conversations.id,
        {"tipo": "SOLICITAR_REGISTRO", "payload": {}},
    )

    assert result.code == "OK"
    assert result.blocks == ({"tipo": "FORMULARIO", "formulario": "REGISTRO"},)
    assert [row["role"] for row in messages.rows] == [MessageRole.TOOL, MessageRole.ASSISTANT]
    assert messages.rows[0]["tool_name"] == "SOLICITAR_REGISTRO"
    assert messages.rows[0]["result_code"] == "OK"
    assert messages.rows[1]["blocks"] == {"blocks": list(result.blocks)}
    assert conversations.touched == 1 and uow.commits == 1 and uow.rollbacks == 0


@pytest.mark.asyncio
async def test_direct_action_hides_missing_or_foreign_conversation():
    caller = CallerIdentity(anonymous_sid="sid-hash")
    other = CallerIdentity(anonymous_sid="other-hash")
    use_case, conversations, messages, uow = make_use_case(caller)

    with pytest.raises(ConversationNotFoundError):
        await use_case.execute(
            other,
            conversations.id,
            {"tipo": "SOLICITAR_REGISTRO", "payload": {}},
        )

    assert messages.rows == [] and uow.commits == uow.rollbacks == 0


@pytest.mark.asyncio
async def test_direct_action_reports_unregistered_tools_without_fabricating_results():
    caller = CallerIdentity(anonymous_sid="sid-hash")
    use_case, conversations, messages, _ = make_use_case(caller)

    result = await use_case.execute(
        caller,
        conversations.id,
        {"tipo": "VER_CARRITO", "payload": {}},
    )

    assert result.code == "ACCION_DESCONOCIDA"
    assert result.blocks[0] == {
        "id": result.blocks[0]["id"],
        "tipo": "ERROR",
        "code": "ACCION_DESCONOCIDA",
        "mensaje": "No se pudo completar la acción.",
    }
    assert messages.rows[-1]["blocks"] == {"blocks": list(result.blocks)}
