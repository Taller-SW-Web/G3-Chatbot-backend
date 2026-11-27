"""CLI: purge local evidence references of abandoned drafts after 24 h (SPEC-21).

The file itself stays in Ventas; only the chatbot's URL row is deleted. This is a
module-owned entrypoint because `src/shared/` and the shared outbox worker are out
of scope for this change. Schedule it externally (cron/K8s CronJob) with:

    python -m src.modules.mod_postventa.jobs.purge_drafts
"""
import argparse
import asyncio
import sys
from datetime import datetime, timezone

from src.modules.mod_postventa.adapters.evidence_postgres_adapter import EvidencePostgresAdapter
from src.modules.mod_postventa.application.postventa_service import DRAFT_TTL
from src.shared.infrastructure.db.connection import open_session


def draft_cutoff(now: datetime) -> datetime:
    """Rows created before this instant crossed the 24 h retention window."""
    return now - DRAFT_TTL


async def purge_evidence_drafts(now: datetime | None = None) -> int:
    """Delete unlinked evidence references older than 24 h; returns rows removed."""
    cutoff = draft_cutoff(now or datetime.now(timezone.utc))
    async with open_session() as session:
        removed = await EvidencePostgresAdapter(session).purgar_borradores(cutoff)
        await session.commit()
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Purge abandoned post-sale evidence drafts.")
    parser.parse_args(argv)
    removed = asyncio.run(purge_evidence_drafts())
    print(f"purge-postventa-drafts: removed {removed} evidence draft(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
