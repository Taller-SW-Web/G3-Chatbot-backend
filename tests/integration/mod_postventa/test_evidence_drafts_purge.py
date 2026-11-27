"""Real PostgreSQL purge of abandoned evidence drafts (SPEC-21, needs TEST_DATABASE_URL)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select

from src.modules.mod_postventa.adapters.evidence_postgres_adapter import EvidencePostgresAdapter
from src.shared.infrastructure.db.models import Evidence

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _conversation(session, metadata) -> uuid.UUID:
    conversation_id = uuid.uuid4()
    await session.execute(
        insert(metadata.tables["conversation"]).values(
            id=conversation_id, anonymous_sid=f"purge_{conversation_id.hex}"
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
