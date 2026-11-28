"""PostgreSQL integration tests for conversation retention (SPEC-05, #252)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.modules.mod_chat.application.conversation_retention_service import (
    ConversationRetentionService,
)
from src.shared.infrastructure.db.models import Conversation
from src.shared.infrastructure.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)
from src.shared.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_retention_closes_old_anonymous_and_archives_old_customer_conversations(
    postgres_session,
) -> None:
    now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
    adapter = ConversationPostgresAdapter(postgres_session)

    stale_anon = await adapter.guardar(
        Conversation(
            anonymous_sid=f"sid-old-{uuid.uuid4()}",
            status="ACTIVE",
            last_message_at=now - timedelta(days=8),
        )
    )
    fresh_anon = await adapter.guardar(
        Conversation(
            anonymous_sid=f"sid-fresh-{uuid.uuid4()}",
            status="ACTIVE",
            last_message_at=now - timedelta(days=3),
        )
    )
    stale_customer = await adapter.guardar(
        Conversation(
            customer_id=uuid.uuid4(),
            status="ACTIVE",
            last_message_at=now - timedelta(days=95),
        )
    )
    fresh_customer = await adapter.guardar(
        Conversation(
            customer_id=uuid.uuid4(),
            status="ACTIVE",
            last_message_at=now - timedelta(days=30),
        )
    )
    already_closed = await adapter.guardar(
        Conversation(
            anonymous_sid=f"sid-closed-{uuid.uuid4()}",
            status="CLOSED",
            last_message_at=now - timedelta(days=20),
        )
    )
    await postgres_session.commit()

    service = ConversationRetentionService(
        conversations=adapter,
        uow=SqlAlchemyUnitOfWork(postgres_session),
        clock=lambda: now,
    )
    closed_count, archived_count = await service.run()

    assert closed_count == 1
    assert archived_count == 1

    await postgres_session.refresh(stale_anon)
    await postgres_session.refresh(fresh_anon)
    await postgres_session.refresh(stale_customer)
    await postgres_session.refresh(fresh_customer)
    await postgres_session.refresh(already_closed)

    assert stale_anon.status == "CLOSED"
    assert fresh_anon.status == "ACTIVE"
    assert stale_customer.status == "ARCHIVED"
    assert fresh_customer.status == "ACTIVE"
    assert already_closed.status == "CLOSED"
