"""Engine/sesión async + Base declarativa.

Decisión: PostgreSQL 18 + Python 3.12.7.
- IDs con uuidv7() nativo de PG18 vía server_default en cada modelo.
- Async con asyncpg (estándar FastAPI + httpx async del proyecto).
- DATABASE_URL se lee de entorno, ej:
  postgresql+asyncpg://postgres:postgres@localhost:5432/chatbot
  En Supabase (pooler): postgresql+asyncpg://postgres.PROYECTO:CLAVE@aws-0-region.pooler.supabase.com:5432/postgres
"""
import json
import os
from collections.abc import AsyncIterator
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        # Valor solo para que Alembic/imports no revienten sin env. Reemplazar por env real.
        return "postgresql+asyncpg://postgres:postgres@localhost:5432/chatbot"
    return url


def _json_serializer(value) -> str:
    # Same rule as the SSE frames (`sse.frame`): Decimal prices are stored as strings,
    # so a persisted block matches what the client received live.
    return json.dumps(value, ensure_ascii=False, default=str)


def _unique_statement_name() -> str:
    return f"__asyncpg_{uuid4()}__"


# The Supabase pooler (Supavisor, PgBouncer-like) shares server connections between
# clients, so cached or sequentially named prepared statements collide (SQLSTATE 42P05).
# See SQLAlchemy docs: "Prepared Statement Name with PGBouncer".
_POOLER_SAFE_CONNECT_ARGS = {
    "statement_cache_size": 0,
    "prepared_statement_cache_size": 0,
    "prepared_statement_name_func": _unique_statement_name,
}


def create_engine(url: str) -> AsyncEngine:
    """Engine shared by the app and the integration tests."""
    return create_async_engine(
        url,
        pool_pre_ping=True,
        json_serializer=_json_serializer,
        connect_args=dict(_POOLER_SAFE_CONNECT_ARGS),
    )


engine: AsyncEngine | None = None
AsyncSessionLocal: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global engine, AsyncSessionLocal
    if engine is None:
        engine = create_engine(get_database_url())
        AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Sessionmaker listo para usar; construye el engine la primera vez."""
    get_engine()
    factory = AsyncSessionLocal
    assert factory is not None  # `get_engine()` siempre lo crea
    return factory


def open_session() -> AsyncSession:
    """Nueva sesión; quien la abre la cierra (`async with connection.open_session()`)."""
    return get_session_factory()()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with open_session() as session:
        yield session
