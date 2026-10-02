"""Engine/sesión async + Base declarativa (H2-01).

Decisión H2-01: PostgreSQL 18 + Python 3.12.7.
- IDs con uuidv7() nativo de PG18 vía server_default en cada modelo.
- Async con asyncpg (estándar FastAPI + httpx async del proyecto).
- DATABASE_URL se lee de entorno, ej:
  postgresql+asyncpg://postgres:postgres@localhost:5432/chatbot
  En Supabase (pooler): postgresql+asyncpg://postgres.PROYECTO:CLAVE@aws-0-region.pooler.supabase.com:5432/postgres
"""
import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        # Valor solo para que Alembic/imports no revienten sin env. Reemplazar por env real.
        return "postgresql+asyncpg://postgres:postgres@localhost:5432/chatbot"
    return url


engine = None
AsyncSessionLocal = None


def get_engine():
    global engine, AsyncSessionLocal
    if engine is None:
        engine = create_async_engine(get_database_url(), pool_pre_ping=True)
        AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine


async def get_session():
    get_engine()
    async with AsyncSessionLocal() as session:
        yield session
