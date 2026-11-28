"""Pure 24 h retention rules for drafts stored in `conversation.context`.

SPEC-19/21: the draft is kept for 24 h after the failed registration; after that
its local reference must disappear. This module has no framework or DB imports so
the service, the PostgreSQL adapter and the purge job share one implementation.
"""
from datetime import datetime, timedelta, timezone
from typing import Any

DRAFT_TTL = timedelta(hours=24)
RETURN_DRAFT_CONTEXT_KEY = "postventaReturnDraft"
CLAIM_DRAFT_CONTEXT_KEY = "postventaClaimDraft"
DRAFT_CONTEXT_KEYS = (RETURN_DRAFT_CONTEXT_KEY, CLAIM_DRAFT_CONTEXT_KEY)


def draft_cutoff(now: datetime, ttl: timedelta = DRAFT_TTL) -> datetime:
    """Rows created before this instant crossed the retention window."""
    return now - ttl


def parse_saved_at(value: Any) -> datetime | None:
    """Parse the ISO timestamp written by the service; None when malformed."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def draft_saved_at(entry: Any) -> datetime | None:
    """Timestamp of a draft entry, or None when the entry is not a draft."""
    if not isinstance(entry, dict):
        return None
    return parse_saved_at(entry.get("savedAt"))


def is_draft_expired(entry: Any, now: datetime, ttl: timedelta = DRAFT_TTL) -> bool:
    """True only when the draft has a readable timestamp older than `ttl`."""
    saved_at = draft_saved_at(entry)
    if saved_at is None:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now - saved_at > ttl


def remove_expired_drafts(
    context: dict[str, Any], now: datetime, ttl: timedelta = DRAFT_TTL
) -> int:
    """Drop the expired post-sale draft keys from a conversation context.

    Mutates `context` in place and returns how many keys were removed. Keys of
    other modules and fresh drafts are preserved.
    """
    removed = 0
    for key in DRAFT_CONTEXT_KEYS:
        entry = context.get(key)
        if entry is not None and is_draft_expired(entry, now, ttl):
            del context[key]
            removed += 1
    return removed
