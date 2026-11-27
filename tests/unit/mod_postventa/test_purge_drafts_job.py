"""Cutoff of the module-owned 24 h purge job (SPEC-21)."""
from datetime import datetime, timedelta, timezone

from src.modules.mod_postventa.jobs.purge_drafts import draft_cutoff


def test_purge_cutoff_is_exactly_24_hours():
    now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
    assert draft_cutoff(now) == now - timedelta(hours=24)
