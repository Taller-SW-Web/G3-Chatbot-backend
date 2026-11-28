"""Periodic purge of the local post-sale drafts (SPEC-19/21, #265).

Deletes only the chatbot's local retention: evidence references still unlinked
after 24 h and the expired `postventa*Draft` keys in `conversation.context`
(the claim draft carries consumer PII). The file itself stays in Ventas. The
clock and the unit of work are injected, so `outbox_worker_lifespan`, the CLI
and the tests share one implementation and can advance time deterministically.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from src.modules.mod_postventa.domain.draft_retention import DRAFT_TTL, draft_cutoff
from src.modules.mod_postventa.ports.draft_retention_port import DraftRetentionPort
from src.shared.application.unit_of_work_port import UnitOfWorkPort
from src.shared.domain.request_context import correlation_id_var

logger = logging.getLogger(__name__)


class DraftPurgeService:
    def __init__(
        self,
        evidence: DraftRetentionPort,
        uow: UnitOfWorkPort,
        *,
        clock: Callable[[], datetime] | None = None,
        ttl: timedelta = DRAFT_TTL,
    ) -> None:
        self._evidence = evidence
        self._uow = uow
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ttl = ttl

    async def run(self) -> tuple[int, int]:
        """Run one idempotent sweep and return `(evidence_rows, context_keys)`."""
        now = self._clock()
        correlation_id = correlation_id_var.get() or str(uuid.uuid4())
        try:
            evidence = await self._evidence.purgar_borradores(draft_cutoff(now, self._ttl))
            context = await self._evidence.purgar_borradores_contexto(now)
            await self._uow.commit()
        except BaseException:
            await self._uow.rollback()
            raise
        logger.info(
            "postventa_draft_purge_completed evidence=%d context=%d correlation_id=%s",
            evidence,
            context,
            correlation_id,
        )
        return evidence, context
