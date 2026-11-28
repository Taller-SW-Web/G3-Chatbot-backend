"""Pure 24 h retention rules for post-sale drafts in conversation.context."""
from datetime import datetime, timedelta, timezone

from src.modules.mod_postventa.domain.draft_retention import (
    CLAIM_DRAFT_CONTEXT_KEY,
    DRAFT_CONTEXT_KEYS,
    DRAFT_TTL,
    RETURN_DRAFT_CONTEXT_KEY,
    draft_saved_at,
    is_draft_expired,
    parse_saved_at,
    remove_expired_drafts,
)

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def test_parse_saved_at_accepts_z_and_naive_values():
    assert parse_saved_at("2026-11-27T12:00:00Z") == NOW
    assert parse_saved_at("2026-11-27T12:00:00") == NOW
    assert parse_saved_at("not-a-date") is None
    assert parse_saved_at(123) is None


def test_draft_expires_strictly_after_24_hours():
    fresh = {"payload": {"pedidoId": "P-1"}, "savedAt": (NOW - DRAFT_TTL).isoformat()}
    expired = {
        "payload": {"pedidoId": "P-1"},
        "savedAt": (NOW - DRAFT_TTL - timedelta(seconds=1)).isoformat(),
    }

    assert draft_saved_at(fresh) == NOW - DRAFT_TTL
    assert is_draft_expired(fresh, NOW) is False
    assert is_draft_expired(expired, NOW) is True


def test_remove_expired_drafts_only_touches_its_own_old_keys():
    context = {
        RETURN_DRAFT_CONTEXT_KEY: {
            "payload": {"pedidoId": "P-1"},
            "savedAt": (NOW - DRAFT_TTL - timedelta(minutes=1)).isoformat(),
        },
        CLAIM_DRAFT_CONTEXT_KEY: {
            "payload": {"pedidoId": "P-2"},
            "savedAt": (NOW - timedelta(hours=1)).isoformat(),
        },
        "crossSellPending": True,
    }

    removed = remove_expired_drafts(context, NOW)

    assert removed == 1
    assert RETURN_DRAFT_CONTEXT_KEY not in context
    assert CLAIM_DRAFT_CONTEXT_KEY in context
    assert context["crossSellPending"] is True


def test_only_the_two_postventa_keys_are_retention_candidates():
    assert set(DRAFT_CONTEXT_KEYS) == {RETURN_DRAFT_CONTEXT_KEY, CLAIM_DRAFT_CONTEXT_KEY}
