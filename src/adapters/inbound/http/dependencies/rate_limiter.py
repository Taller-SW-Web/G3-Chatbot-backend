"""In-memory sliding-window limits for the public registration endpoints (SPEC-01/02).

Three independent rules live here: 5 registrations per IP every 10 minutes
(SPEC-01 RNF), 3 verification re-sends per email every hour (SPEC-02) and a
10-second anti-rebound per email that stops double submits.

LIMITATION: the counters live in the process. With more than one backend
replica each replica keeps its own window, so the effective limit multiplies
by the replica count. Acceptable for the single-instance Hito 3 deployment;
move the counters to a shared store (Redis) before scaling horizontally.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass

from fastapi import Request


@dataclass(frozen=True)
class RateLimitRule:
    name: str
    limit: int
    window_seconds: float


REGISTER_PER_IP = RateLimitRule("registro_por_ip", 5, 10 * 60)
REGISTER_DEBOUNCE_PER_EMAIL = RateLimitRule("registro_antirrebote", 1, 10)
RESEND_PER_EMAIL = RateLimitRule("reenvio_por_correo", 3, 60 * 60)


def client_ip(request: Request) -> str:
    """Client IP behind the ADR-0023 proxy: first `X-Forwarded-For` hop, else the peer."""
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    if first:
        return first
    return request.client.host if request.client else "unknown"


class InMemoryRateLimiter:
    """Sliding window per (rule, key). `hit` records one event unless blocked."""

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._events: dict[tuple[str, str], deque[float]] = {}
        self._lock = asyncio.Lock()

    async def hit(self, rule: RateLimitRule, key: str) -> int | None:
        """Returns `retry_after` seconds when the limit is already reached."""
        now = self._clock()
        async with self._lock:
            return self._consume(rule, key, now)

    async def hit_any(
        self, checks: list[tuple[RateLimitRule, str]]
    ) -> tuple[RateLimitRule, int] | None:
        """All-or-nothing over several rules: records every event only if all pass.

        Used by `/registro` so a request blocked by the IP limit does not burn
        the email anti-rebound (and vice versa).
        """
        now = self._clock()
        async with self._lock:
            for rule, key in checks:
                bucket = self._prune(rule, key, now)
                if len(bucket) >= rule.limit:
                    retry = max(1, math.ceil(bucket[0] + rule.window_seconds - now))
                    return rule, retry
            for rule, key in checks:
                self._events.setdefault((rule.name, key), deque()).append(now)
            return None

    def reset(self) -> None:
        """Test helper: forget every window."""
        self._events.clear()

    def _prune(self, rule: RateLimitRule, key: str, now: float) -> deque[float]:
        bucket = self._events.setdefault((rule.name, key), deque())
        cutoff = now - rule.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        return bucket

    def _consume(self, rule: RateLimitRule, key: str, now: float) -> int | None:
        bucket = self._prune(rule, key, now)
        if len(bucket) >= rule.limit:
            return max(1, math.ceil(bucket[0] + rule.window_seconds - now))
        bucket.append(now)
        return None
