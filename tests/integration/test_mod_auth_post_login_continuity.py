"""PostgreSQL 18 integration tests for post-login continuity (SPEC-03/05, #246)."""
from __future__ import annotations

import uuid

import pytest

from src.modules.mod_auth.application.manage_session_use_case import ManageSessionUseCase
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.infrastructure.db.models import Conversation
from src.shared.infrastructure.http.dependencies.caller_identity import hash_chat_sid
from src.shared.infrastructure.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)
from src.shared.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class _FakeSecurity:
    async def login(self, email: str, password: str) -> dict:
        return {"accessToken": "acc-token", "refreshToken": "ref-token", "expiresIn": 900}

    async def logout(self, refresh_token: str) -> None:
        return None


class _FakeValidator:
    def __init__(self, customer_id: uuid.UUID) -> None:
        self._customer_id = customer_id

    async def validate_customer(self, token: str) -> dict:
        return {"sub": str(self._customer_id), "roles": ["CLIENTE"]}


async def test_login_links_anonymous_conversation_to_customer_and_consumes_pending_action(
    postgres_session,
) -> None:
    customer_id = uuid.uuid4()
    hashed_sid = hash_chat_sid(f"sid-{uuid.uuid4()}")
    adapter = ConversationPostgresAdapter(postgres_session)

    anon_conv = await adapter.guardar(
        Conversation(
            anonymous_sid=hashed_sid,
            title="Zapatillas running",
            status="ACTIVE",
            context={"accionPendiente": "INICIAR_CHECKOUT", "filtros": {"talla": "42"}},
        )
    )
    await postgres_session.commit()

    uc = ManageSessionUseCase(
        security=_FakeSecurity(),
        validator=_FakeValidator(customer_id),
        conversations=adapter,
        uow=SqlAlchemyUnitOfWork(postgres_session),
    )

    first = await uc.login("cliente@correo.pe", "Secret123!", anonymous_sid=hashed_sid)
    assert first["accionPendiente"] == "INICIAR_CHECKOUT"

    customer_owner = CallerIdentity(customer_id=customer_id)
    owned = await adapter.find_owned(anon_conv.id, customer_owner)
    assert owned is not None
    assert owned.id == anon_conv.id

    summaries = await adapter.list_summaries(customer_owner, limit=10, offset=0)
    assert [s.id for s in summaries] == [anon_conv.id]

    ctx = await adapter.get_context(anon_conv.id)
    assert "accionPendiente" not in ctx
    assert ctx["filtros"] == {"talla": "42"}

    # Second login does not re-emit accionPendiente
    second = await uc.login("cliente@correo.pe", "Secret123!", anonymous_sid=hashed_sid)
    assert "accionPendiente" not in second
