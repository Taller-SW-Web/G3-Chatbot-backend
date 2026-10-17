"""Sliding-window rate limiter for the public registration endpoints (SPEC-01/02)."""
import pytest
from starlette.requests import Request

from src.adapters.inbound.http.dependencies.rate_limiter import (
    REGISTER_DEBOUNCE_PER_EMAIL,
    REGISTER_PER_IP,
    RESEND_PER_EMAIL,
    InMemoryRateLimiter,
    RateLimitRule,
    client_ip,
)


def make_request(headers: dict | None = None, client=("10.0.0.1", 1234)) -> Request:
    scope = {
        "type": "http",
        "headers": [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()],
        "client": client,
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_allows_up_to_the_limit_then_reports_retry_after():
    now = [0.0]
    limiter = InMemoryRateLimiter(clock=lambda: now[0])
    rule = RateLimitRule("test", limit=2, window_seconds=60)
    assert await limiter.hit(rule, "k") is None
    assert await limiter.hit(rule, "k") is None
    assert await limiter.hit(rule, "k") == 60
    now[0] += 30
    assert await limiter.hit(rule, "k") == 30


@pytest.mark.asyncio
async def test_events_expire_after_the_window():
    now = [0.0]
    limiter = InMemoryRateLimiter(clock=lambda: now[0])
    rule = RateLimitRule("test", limit=1, window_seconds=10)
    assert await limiter.hit(rule, "k") is None
    assert await limiter.hit(rule, "k") is not None
    now[0] += 10.1
    assert await limiter.hit(rule, "k") is None


@pytest.mark.asyncio
async def test_keys_and_rules_do_not_share_counters():
    limiter = InMemoryRateLimiter(clock=lambda: 0.0)
    rule = RateLimitRule("test", limit=1, window_seconds=60)
    assert await limiter.hit(rule, "a") is None
    assert await limiter.hit(rule, "b") is None
    assert await limiter.hit(REGISTER_PER_IP, "a") is None


@pytest.mark.asyncio
async def test_the_shipped_rules_match_the_spec():
    """SPEC-01: 5/IP/10 min; SPEC-02: 3/email/hour; anti-rebound 10 s."""
    assert (REGISTER_PER_IP.limit, REGISTER_PER_IP.window_seconds) == (5, 600)
    assert (RESEND_PER_EMAIL.limit, RESEND_PER_EMAIL.window_seconds) == (3, 3600)
    assert (REGISTER_DEBOUNCE_PER_EMAIL.limit, REGISTER_DEBOUNCE_PER_EMAIL.window_seconds) == (1, 10)


@pytest.mark.asyncio
async def test_hit_any_records_nothing_when_one_rule_blocks():
    limiter = InMemoryRateLimiter(clock=lambda: 0.0)
    email_rule = RateLimitRule("email", limit=1, window_seconds=60)
    ip_rule = RateLimitRule("ip", limit=2, window_seconds=60)
    assert await limiter.hit_any([(email_rule, "a@b.pe"), (ip_rule, "10.0.0.1")]) is None
    assert await limiter.hit_any([(email_rule, "a@b.pe"), (ip_rule, "10.0.0.1")]) is not None
    # The blocked request must not have consumed an IP slot.
    assert await limiter.hit(ip_rule, "10.0.0.1") is None


def test_client_ip_prefers_the_first_forwarded_hop():
    """ADR-0023 - the backend sits behind the same-origin proxy."""
    assert client_ip(make_request({"X-Forwarded-For": "203.0.113.7, 10.0.0.1"})) == "203.0.113.7"


def test_client_ip_falls_back_to_the_peer():
    assert client_ip(make_request()) == "10.0.0.1"
