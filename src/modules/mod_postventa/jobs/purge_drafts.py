"""CLI: purge expired draft references after 24 h (SPEC-19/21).

It removes both kinds of local retention:
- `evidence` rows of abandoned uploads without `return_id`;
- `postventaReturnDraft` / `postventaClaimDraft` keys in `conversation.context`
  (the claim draft carries consumer PII).

The file itself stays in Ventas; only the chatbot's local data is deleted. This is
a module-owned entrypoint because `src/shared/` and the shared outbox worker are
out of scope. Schedule it externally (cron/K8s CronJob), tracked in #265:

    python -m src.modules.mod_postventa.jobs.purge_drafts
"""
import argparse
import asyncio
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from src.modules.mod_postventa.adapters.evidence_postgres_adapter import EvidencePostgresAdapter
from src.modules.mod_postventa.domain.draft_retention import DRAFT_TTL
from src.shared.infrastructure.db.connection import open_session


def draft_cutoff(now: datetime) -> datetime:
    """Rows created before this instant crossed the 24 h retention window."""
    return now - DRAFT_TTL


async def purge_drafts(
    now: datetime | None = None,
    *,
    session_factory: Callable[[], Any] = open_session,
) -> tuple[int, int]:
    """Purge evidence rows and context draft keys; returns `(evidence, context)`."""
    current = now or datetime.now(timezone.utc)
    async with session_factory() as session:
        adapter = EvidencePostgresAdapter(session)
        evidence = await adapter.purgar_borradores(draft_cutoff(current))
        context = await adapter.purgar_borradores_contexto(current)
        await session.commit()
    return evidence, context


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
