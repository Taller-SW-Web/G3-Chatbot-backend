"""Tests H2-03 (Sonny): modelo outbox y adapter sin BD (offline).

Patron transaccional ADR-0011: el INSERT comparte transaccion con el
intento de pago; el worker usa listar_pendientes con SKIP LOCKED.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models import Base, Outbox
from src.adapters.outbound.persistence.outbox_postgres_adapter import OutboxPostgresAdapter

table = Base.metadata.tables["outbox"]


def _checks():
    return [str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)]


def _index(name):
    return next(i for i in table.indexes if i.name == name)


def test_modelo_registrado_en_base():
    assert Outbox.__tablename__ == "outbox"
    assert "outbox" in Base.metadata.tables


def test_columnas_y_nulabilidad():
    assert set(table.columns.keys()) == {
        "id", "type", "payload", "status", "attempts", "next_attempt_at",
        "last_attempt_at", "last_error", "processed_at", "created_at", "updated_at",
    }
    for nullable in ("last_attempt_at", "last_error", "processed_at"):
        assert table.columns[nullable].nullable is True
    for required in ("type", "payload", "status", "attempts",
                     "next_attempt_at", "created_at", "updated_at"):
        assert table.columns[required].nullable is False
    assert "uuidv7()" in str(table.columns["id"].server_default.arg)
    assert "PENDING" in str(table.columns["status"].server_default.arg)
    assert str(table.columns["attempts"].server_default.arg) == "0"


def test_sin_fks():
    assert not table.foreign_keys


def test_check_constraints():
    checks = _checks()
    assert any("NOTIFY_SALES_PAYMENT" in s and "SEND_EMAIL" in s for s in checks)
    assert any("jsonb_typeof(payload)" in s for s in checks)
    assert any("PENDING" in s and "FAILED" in s for s in checks)
    assert any("attempts BETWEEN 0 AND 5" in s for s in checks)


def test_indices_worker_y_admin():
    polling = _index("ix_outbox_next_attempt_at")
    assert list(polling.columns.keys()) == ["next_attempt_at"]
    assert str(polling.dialect_options["postgresql"]["where"]) == "status = 'PENDING'"
    fallidos = _index("ix_outbox_failed_created_at")
    assert str(fallidos.dialect_options["postgresql"]["where"]) == "status = 'FAILED'"


def test_adapter_expone_operaciones():
    for method in ("guardar", "obtener_por_id", "listar_pendientes",
                   "marcar_procesado", "marcar_reintento"):
        assert hasattr(OutboxPostgresAdapter, method)


@pytest.fixture
def session():
    return MagicMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_guardar_hace_flush_sin_commit(session):
    """SPEC-15 Req.2: notificacion en la misma tx que el intento de pago."""
    row = Outbox(type="NOTIFY_SALES_PAYMENT", payload={"pedidoId": "PED-1"})
    assert await OutboxPostgresAdapter(session).guardar(row) is row
    session.add.assert_called_once_with(row)
    session.flush.assert_awaited_once_with()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_listar_pendientes_usa_skip_locked(session):
    """ADR-0011: mas de un worker sin tomar la misma tarea."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result
    assert await OutboxPostgresAdapter(session).listar_pendientes() == []
    stmt = session.execute.call_args.args[0]
    compiled = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE SKIP LOCKED" in compiled
    assert "outbox.status = 'PENDING'" in compiled


@pytest.mark.asyncio
async def test_quinto_reintento_marca_failed(session):
    row = Outbox(id=uuid.uuid4(), type="SEND_EMAIL", payload={}, attempts=4, status="PENDING")
    adapter = OutboxPostgresAdapter(session)
    adapter.obtener_por_id = MagicMock(return_value=row)  # type: ignore[method-assign]
    # obtener_por_id mockeado como async
    async def _obtener(_id):
        return row
    adapter.obtener_por_id = _obtener  # type: ignore[method-assign]
    out = await adapter.marcar_reintento(row.id, datetime.now(timezone.utc), "timeout")
    assert out is row
    assert row.attempts == 5
    assert row.status == "FAILED"
    session.flush.assert_awaited_once_with()
    session.commit.assert_not_awaited()
