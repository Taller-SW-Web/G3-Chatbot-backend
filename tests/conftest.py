"""Fixtures H2-06: modelos reales; return_ref mínimo existe solo en tests."""

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import Column, MetaData, Table, Text, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.adapters.outbound.persistence.models import Attachment, Conversation, Evidence, Message


@pytest.fixture
def persistence_metadata():
    """Metadata aislada: no altera Base ni registra modelos de otros autores."""
    metadata = MetaData()
    Conversation.__table__.to_metadata(metadata)
    Table("return_ref", metadata, Column("return_id", Text, primary_key=True))
    Message.__table__.to_metadata(metadata)
    Evidence.__table__.to_metadata(metadata)
    Attachment.__table__.to_metadata(metadata)
    return metadata


@pytest_asyncio.fixture
async def postgres_session(persistence_metadata):
    """PostgreSQL 18 de pruebas explícito; DDL y datos se revierten al terminar.

    Nunca usa DATABASE_URL ni carga .env. return_ref mínimo y el trigger
    aquí permiten verificar H2-06; no sustituyen la migración compartida.
    """
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Falta TEST_DATABASE_URL: integración PostgreSQL 18 no ejecutada")
    engine = create_async_engine(url)
    schema = f"h2_06_test_{uuid.uuid4().hex}"
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                version = await connection.scalar(text("SHOW server_version_num"))
                if int(version) < 180000:
                    pytest.fail("H2-06 requiere PostgreSQL 18+ con uuidv7() nativo")
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                await connection.run_sync(persistence_metadata.create_all)
                await connection.execute(text("""
                    CREATE FUNCTION set_updated_at() RETURNS trigger
                    LANGUAGE plpgsql AS $$
                    BEGIN
                        NEW.updated_at = now();
                        RETURN NEW;
                    END;
                    $$
                """))
                await connection.execute(text("""
                    CREATE TRIGGER evidence_updated_at
                    BEFORE UPDATE ON evidence FOR EACH ROW
                    EXECUTE FUNCTION set_updated_at()
                """))
                async with AsyncSession(
                    bind=connection, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                ) as session:
                    yield session
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
