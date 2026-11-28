"""Cutoff and both purges of the module-owned 24 h job (SPEC-19/21)."""
import uuid
from datetime import datetime, timedelta, timezone

from src.modules.mod_postventa.domain.draft_retention import (
    CLAIM_DRAFT_CONTEXT_KEY,
    DRAFT_TTL,
    RETURN_DRAFT_CONTEXT_KEY,
)
from src.modules.mod_postventa.jobs.purge_drafts import draft_cutoff, purge_drafts
from src.shared.infrastructure.db.models import Conversation

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def test_purge_cutoff_is_exactly_24_hours():
    assert draft_cutoff(NOW) == NOW - timedelta(hours=24)


class _DeleteResult:
    rowcount = 2


class _SelectResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _JobSession:
    def __init__(self, conversations):
        self.conversations = conversations
        self.commits = 0
        self.flushes = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, stmt):
        if getattr(stmt, "is_delete", False):
            return _DeleteResult()
        return _SelectResult(self.conversations)

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1


def test_job_purges_evidence_and_expired_context_drafts():
    old = Conversation(
        id=uuid.uuid4(),
        anonymous_sid="old",
        context={
            RETURN_DRAFT_CONTEXT_KEY: {
                "payload": {"pedidoId": "P-1"},
                "savedAt": (NOW - DRAFT_TTL - timedelta(minutes=1)).isoformat(),
            },
        },
    )
    fresh = Conversation(
        id=uuid.uuid4(),
        anonymous_sid="fresh",
        context={
            CLAIM_DRAFT_CONTEXT_KEY: {
                "payload": {"pedidoId": "P-2"},
                "savedAt": (NOW - timedelta(hours=1)).isoformat(),
            },
        },
    )
    session = _JobSession([old, fresh])

    import anyio

    evidence, context = anyio.run(
        lambda: purge_drafts(NOW, session_factory=lambda: session)
    )

    assert (evidence, context) == (2, 1)
    assert RETURN_DRAFT_CONTEXT_KEY not in old.context
    assert CLAIM_DRAFT_CONTEXT_KEY in fresh.context
    assert session.commits == 1
    assert session.flushes == 1
