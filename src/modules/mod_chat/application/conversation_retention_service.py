"""Conversation retention job (SPEC-05, #252).

Closes stale anonymous conversations (>7 days without messages) and archives
inactive customer conversations (>90 days). Uses an injectable clock so tests
can simulate elapsed time deterministically.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from src.shared.application.unit_of_work_port import UnitOfWorkPort

logger = logging.getLogger(__name__)

DEFAULT_ANONYMOUS_TTL_DAYS = 7
DEFAULT_CUSTOMER_ARCHIVE_DAYS = 90


class ConversationRetentionRepository(Protocol):
    async def apply_retention(
        self,
        *,
        now: datetime,
        anonymous_ttl_days: int = DEFAULT_ANONYMOUS_TTL_DAYS,
        customer_archive_days: int = DEFAULT_CUSTOMER_ARCHIVE_DAYS,
    ) -> tuple[int, int]: ...


class ConversationRetentionService:
    def __init__(
        self,
        conversations: ConversationRetentionRepository,
        uow: UnitOfWorkPort,
        *,
        clock: Callable[[], datetime] | None = None,
        anonymous_ttl_days: int = DEFAULT_ANONYMOUS_TTL_DAYS,
        customer_archive_days: int = DEFAULT_CUSTOMER_ARCHIVE_DAYS,
    ) -> None:
        self._conversations = conversations
        self._uow = uow
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._anonymous_ttl_days = anonymous_ttl_days
        self._customer_archive_days = customer_archive_days

    async def run(self) -> tuple[int, int]:
        """Run one retention sweep and return `(closed_anonymous, archived_customer)`."""
        now = self._clock()
        try:
            closed, archived = await self._conversations.apply_retention(
                now=now,
                anonymous_ttl_days=self._anonymous_ttl_days,
                customer_archive_days=self._customer_archive_days,
            )
            await self._uow.commit()
        except BaseException:
            await self._uow.rollback()
            raise
        if closed or archived:
            logger.info(
                "conversation_retention_completed closed_anonymous=%d archived_customer=%d",
                closed,
                archived,
            )
        return closed, archived
