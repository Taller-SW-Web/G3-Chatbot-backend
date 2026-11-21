"""The outbox worker delivers internal feedback e-mails (SPEC-24 Req. 5)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.shared.infrastructure.worker.outbox_worker import OutboxProcessor


@pytest.mark.asyncio
async def test_send_feedback_email_calls_the_notifier_and_closes_the_job():
    outbox = MagicMock()
    outbox.marcar_procesado = AsyncMock()
    notifier = MagicMock()
    notifier.send = AsyncMock()
    processor = OutboxProcessor(
        outbox_repo=outbox,
        order_ref_repo=MagicMock(),
        sales_client=MagicMock(),
        feedback_notifier=notifier,
    )
    job = SimpleNamespace(id="job-1", type="SEND_FEEDBACK_EMAIL", payload={"titulo": "Bug"}, attempts=0)

    assert await processor.process_one(job) is True

    notifier.send.assert_awaited_once_with({"titulo": "Bug"})
    outbox.marcar_procesado.assert_awaited_once_with("job-1")


@pytest.mark.asyncio
async def test_missing_notifier_schedules_a_retry_instead_of_dropping_the_job():
    outbox = MagicMock()
    outbox.marcar_reintento = AsyncMock()
    processor = OutboxProcessor(
        outbox_repo=outbox,
        order_ref_repo=MagicMock(),
        sales_client=MagicMock(),
    )
    job = SimpleNamespace(id="job-2", type="SEND_FEEDBACK_EMAIL", payload={}, attempts=0)

    assert await processor.process_one(job) is False
    outbox.marcar_reintento.assert_awaited_once()
