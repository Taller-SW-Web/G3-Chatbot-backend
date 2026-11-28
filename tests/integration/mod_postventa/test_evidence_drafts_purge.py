"""Real PostgreSQL purge of abandoned evidence drafts (SPEC-21, needs TEST_DATABASE_URL)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select

from src.modules.mod_postventa.adapters.evidence_postgres_adapter import EvidencePostgresAdapter
from src.modules.mod_postventa.application.draft_purge_service import DraftPurgeService
from src.modules.mod_postventa.domain.draft_retention import (
    CLAIM_DRAFT_CONTEXT_KEY,
    DRAFT_TTL,
    RETURN_DRAFT_CONTEXT_KEY,
)
from src.shared.infrastructure.db.models import Conversation, Evidence
from src.shared.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _conversation(session, metadata, context=None) -> uuid.UUID:
    conversation_id = uuid.uuid4()
    await session.execute(
        insert(metadata.tables["conversation"]).values(
            id=conversation_id,
            anonymous_sid=f"purge_{conversation_id.hex}",
            context=context or {},
        )
    )
    return conversation_id


def _evidence(conversation_id: uuid.UUID, url: str, created_at: datetime, return_id=None):
    return Evidence(
        conversation_id=conversation_id,
        return_id=return_id,
        type="IMAGE",
        url=url,
        original_file_name="foto.jpg",
        size_bytes=1024,
        created_at=created_at,
    )


async def test_purge_only_removes_unlinked_drafts_older_than_24_hours(
    postgres_session, persistence_metadata
):
    session = postgres_session
    conversation_id = await _conversation(session, persistence_metadata)
    await session.execute(
        insert(persistence_metadata.tables["return_ref"]).values(return_id="DEV-OLD")
    )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    adapter = EvidencePostgresAdapter(session)

    old_draft = await adapter.guardar(
        _evidence(conversation_id, "https://ventas.example/old.jpg", cutoff - timedelta(minutes=1))
    )
    boundary = await adapter.guardar(
        _evidence(conversation_id, "https://ventas.example/boundary.jpg", cutoff)
    )
    fresh = await adapter.guardar(
        _evidence(conversation_id, "https://ventas.example/fresh.jpg", cutoff + timedelta(minutes=1))
    )
    linked = await adapter.guardar(
        _evidence(
            conversation_id,
            "https://ventas.example/linked.jpg",
            cutoff - timedelta(days=2),
            return_id="DEV-OLD",
        )
    )

    removed = await adapter.purgar_borradores(cutoff)

    remaining = set((await session.execute(select(Evidence.id))).scalars().all())
    assert removed == 1
    assert old_draft.id not in remaining
    assert boundary.id in remaining
    assert fresh.id in remaining
    assert linked.id in remaining


async def test_purge_context_only_removes_expired_postventa_drafts(
    postgres_session, persistence_metadata
):
    session = postgres_session
    now = datetime.now(timezone.utc)
    old_id = await _conversation(
        session,
        persistence_metadata,
        {
            RETURN_DRAFT_CONTEXT_KEY: {
                "payload": {"pedidoId": "P-1"},
                "savedAt": (now - DRAFT_TTL - timedelta(minutes=1)).isoformat(),
            },
        },
    )
    fresh_id = await _conversation(
        session,
        persistence_metadata,
        {
            CLAIM_DRAFT_CONTEXT_KEY: {
                "payload": {"pedidoId": "P-2"},
                "savedAt": (now - timedelta(hours=1)).isoformat(),
            },
        },
    )

    removed = await EvidencePostgresAdapter(session).purgar_borradores_contexto(now)

    old = await session.get(Conversation, old_id)
    fresh = await session.get(Conversation, fresh_id)
    assert removed == 1
    assert RETURN_DRAFT_CONTEXT_KEY not in old.context
    assert CLAIM_DRAFT_CONTEXT_KEY in fresh.context


async def test_service_purges_only_expired_drafts_keeps_linked_and_is_idempotent(
    postgres_session, persistence_metadata
):
    """#265: the scheduled service keeps linked and recent evidence intact."""
    session = postgres_session
    now = datetime.now(timezone.utc)
    conversation_id = await _conversation(
        session,
        persistence_metadata,
        {
            RETURN_DRAFT_CONTEXT_KEY: {
                "payload": {"pedidoId": "P-1"},
                "savedAt": (now - DRAFT_TTL - timedelta(minutes=1)).isoformat(),
            },
            CLAIM_DRAFT_CONTEXT_KEY: {
                "payload": {"pedidoId": "P-2"},
                "savedAt": (now - timedelta(hours=1)).isoformat(),
            },
            "crossSellPending": True,
        },
    )
    await session.execute(
        insert(persistence_metadata.tables["return_ref"]).values(return_id="DEV-KEEP")
    )
    adapter = EvidencePostgresAdapter(session)
    old = await adapter.guardar(
        _evidence(
            conversation_id,
            "https://ventas.example/old.jpg",
            now - DRAFT_TTL - timedelta(minutes=1),
        )
    )
    fresh = await adapter.guardar(
        _evidence(conversation_id, "https://ventas.example/fresh.jpg", now - timedelta(hours=1))
    )
    linked = await adapter.guardar(
        _evidence(
            conversation_id,
            "https://ventas.example/linked.jpg",
            now - DRAFT_TTL - timedelta(days=1),
            return_id="DEV-KEEP",
        )
    )
    service = DraftPurgeService(
        evidence=adapter,
        uow=SqlAlchemyUnitOfWork(session),
        clock=lambda: now,
    )

    assert await service.run() == (1, 1)
    assert await service.run() == (0, 0)

    remaining = set((await session.execute(select(Evidence.id))).scalars().all())
    conversation = await session.get(Conversation, conversation_id)
    assert old.id not in remaining
    assert fresh.id in remaining
    assert linked.id in remaining
    assert RETURN_DRAFT_CONTEXT_KEY not in conversation.context
    assert CLAIM_DRAFT_CONTEXT_KEY in conversation.context
    assert conversation.context["crossSellPending"] is True

