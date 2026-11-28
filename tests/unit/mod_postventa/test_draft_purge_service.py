"""Injectable clock, transaction and idempotency of the 24 h draft purge (SPEC-19/21, #265)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.modules.mod_postventa.application.draft_purge_service import DraftPurgeService
from src.modules.mod_postventa.domain.draft_retention import DRAFT_TTL

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


class _Retention:
    def __init__(self, evidence: int = 2, context: int = 1):
        self.evidence = evidence
        self.context = context
        self.cutoffs: list[datetime] = []
        self.nows: list[datetime] = []
        self.failure: Exception | None = None

    async def purgar_borradores(self, corte: datetime) -> int:
        if self.failure is not None:
            raise self.failure
        self.cutoffs.append(corte)
        return self.evidence

    async def purgar_borradores_contexto(self, ahora: datetime, limite: int = 1000) -> int:
        self.nows.append(ahora)
        return self.context


class _UnitOfWork:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_run_uses_the_injected_clock_and_commits_once(caplog):
    retention, uow = _Retention(), _UnitOfWork()
    service = DraftPurgeService(retention, uow, clock=lambda: NOW)

    with caplog.at_level("INFO"):
        result = await service.run()

    assert result == (2, 1)
    assert retention.cutoffs == [NOW - DRAFT_TTL]
    assert retention.nows == [NOW]
    assert (uow.commits, uow.rollbacks) == (1, 0)
    assert "postventa_draft_purge_completed evidence=2 context=1" in caplog.text
    assert "correlation_id=" in caplog.text


@pytest.mark.asyncio
async def test_a_second_run_is_an_idempotent_no_op():
    retention, uow = _Retention(), _UnitOfWork()
    service = DraftPurgeService(retention, uow, clock=lambda: NOW)

    assert await service.run() == (2, 1)
    retention.evidence = retention.context = 0
    assert await service.run() == (0, 0)
    assert uow.commits == 2
    assert uow.rollbacks == 0


@pytest.mark.asyncio
async def test_a_failed_sweep_rolls_back_and_propagates():
    retention, uow = _Retention(), _UnitOfWork()
    retention.failure = RuntimeError("db unavailable")
    service = DraftPurgeService(retention, uow, clock=lambda: NOW)

    with pytest.raises(RuntimeError, match="db unavailable"):
        await service.run()

    assert (uow.commits, uow.rollbacks) == (0, 1)


@pytest.mark.asyncio
async def test_custom_ttl_shifts_the_cutoff():
    retention, uow = _Retention(), _UnitOfWork()
    service = DraftPurgeService(
        retention, uow, clock=lambda: NOW, ttl=timedelta(hours=6)
    )

    await service.run()

    assert retention.cutoffs == [NOW - timedelta(hours=6)]


def test_configure_registers_the_purge_capability():
    from src.modules.mod_postventa import module
    from src.shared.infrastructure.di.capabilities import POSTVENTA_DRAFT_PURGE

    registered = {}

    class Container:
        def register(self, name, provider):
            registered[name] = provider

    module.configure(Container())

    assert registered[POSTVENTA_DRAFT_PURGE] is module.provide_draft_purge
    assert isinstance(module.provide_draft_purge(object()), DraftPurgeService)
