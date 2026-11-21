"""SQLAlchemy implementation of UnitOfWorkPort: the use case decides commit or rollback.

Driver errors are translated to the persistence port errors here too, so a
conflict raised while committing reaches the use case as `StaleCartError`
(optimistic lock) or `CartConflictError` (unique key), never as a raw
SQLAlchemy exception.
"""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from src.ports.outbound.cart_repository_port import (
    CartConflictError,
    StaleCartError,
)


class SqlAlchemyUnitOfWork:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def commit(self) -> None:
        try:
            await self.session.commit()
        except StaleDataError as exc:
            raise StaleCartError(str(exc)) from exc
        except IntegrityError as exc:
            raise CartConflictError(str(exc)) from exc

    async def rollback(self) -> None:
        await self.session.rollback()
