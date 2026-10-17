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


def create_engine(url: str) -> AsyncEngine:
    """Engine shared by the app and the integration tests."""
    return create_async_engine(url, pool_pre_ping=True, json_serializer=_json_serializer)


engine = None
AsyncSessionLocal = None


def get_engine():
    global engine, AsyncSessionLocal
    if engine is None:
        engine = create_engine(get_database_url())
        AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine


async def get_session():
    get_engine()
    async with AsyncSessionLocal() as session:
        yield session
