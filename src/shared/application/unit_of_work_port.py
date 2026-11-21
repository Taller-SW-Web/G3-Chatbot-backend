"""Outbound port: the use case controls the transaction through this abstraction."""
from typing import Protocol


class UnitOfWorkPort(Protocol):
    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...
