"""CLI/manual entrypoint: purge expired draft references after 24 h (SPEC-19/21).

It removes both kinds of local retention:
- `evidence` rows of abandoned uploads without `return_id`;
- `postventaReturnDraft` / `postventaClaimDraft` keys in `conversation.context`
  (the claim draft carries consumer PII).

Production runs the same `DraftPurgeService` from `outbox_worker_lifespan`
every `POSTVENTA_DRAFT_PURGE_INTERVAL_SECONDS` (#265); this module stays as the
manual/verification entrypoint:

    python -m src.modules.mod_postventa.jobs.purge_drafts
"""
import argparse
import asyncio
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from src.modules.mod_postventa.adapters.evidence_postgres_adapter import EvidencePostgresAdapter
from src.modules.mod_postventa.application.draft_purge_service import DraftPurgeService
from src.modules.mod_postventa.domain.draft_retention import draft_cutoff
from src.shared.infrastructure.db.connection import open_session
from src.shared.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

__all__ = ["draft_cutoff", "main", "purge_drafts"]


async def purge_drafts(
    now: datetime | None = None,
    *,
    session_factory: Callable[[], Any] = open_session,
) -> tuple[int, int]:
    """Purge evidence rows and context draft keys; returns `(evidence, context)`."""
    current = now or datetime.now(timezone.utc)
    async with session_factory() as session:
        service = DraftPurgeService(
            evidence=EvidencePostgresAdapter(session),
            uow=SqlAlchemyUnitOfWork(session),
            clock=lambda: current,
        )
        return await service.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Purge expired post-sale drafts.")
    parser.parse_args(argv)
    evidence, context = asyncio.run(purge_drafts())
    print(
        "purge-postventa-drafts: "
        f"removed {evidence} evidence draft(s) and {context} context draft key(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
