"""Tests H2-04 (David): modelo notification + adapter sin BD (offline).

Trazabilidad:
- SPEC-16 (notificacion-confirmacion) Req. 3: confirmacion + maximo 2
  reenvios CONFIRMATION_RESEND; unicidad (order_id, resend_number).
- docs/modelo-datos.md (seccion `notification`).
"""
from unittest.mock import MagicMock

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models import Base, Notification
from src.adapters.outbound.persistence.notification_postgres_adapter import (
    NotificationPostgresAdapter,
)

table = Base.metadata.tables["notification"]


def _checks():
    return [str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)]


def test_modelo_registrado_en_base():
    assert Notification.__tablename__ == "notification"
    assert "notification" in Base.metadata.tables


def test_columnas_y_nulabilidad():
    assert set(table.columns.keys()) == {
        "id", "order_id", "type", "resend_number", "recipient", "status",
        "attempts", "last_error", "sent_at", "created_at", "updated_at",
    }
    for required in (
        "order_id", "type", "resend_number", "recipient", "status",
        "attempts", "created_at", "updated_at",
    ):
        assert table.columns[required].nullable is False
    for optional in ("last_error", "sent_at"):
        assert table.columns[optional].nullable is True
    assert "PENDING" in str(table.columns["status"].server_default.arg)
    assert table.columns["updated_at"].server_onupdate is not None


def test_fk_order_ref():
    fks = {fk.parent.name: fk for fk in table.foreign_keys}
    assert fks["order_id"].target_fullname == "order_ref.order_id"
    assert fks["order_id"].ondelete == "RESTRICT"


def test_uq_order_resend_idempotencia():
    uniques = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
    assert {"uq_notification_order_resend"} <= {u.name for u in uniques}


def test_check_tipo_resend_estado_intentos():
    checks = _checks()
    assert any("ORDER_CONFIRMATION" in s and "CONFIRMATION_RESEND" in s for s in checks)
    assert any("resend_number" in s and "IN (1, 2)" in s for s in checks)
    assert any("PENDING" in s and "FAILED" in s for s in checks)
    assert any("attempts BETWEEN 0 AND 3" in s for s in checks)


def test_adapter_expone_operaciones():
    for method in (
        "guardar", "obtener_por_order_y_reenvio", "listar_por_order",
        "marcar_enviada", "marcar_fallida",
    ):
        assert hasattr(NotificationPostgresAdapter, method)


@pytest.fixture
def session():
    return MagicMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_guardar_hace_flush_sin_commit(session):
    """SPEC-16: la confirmacion se registra PENDING antes del worker."""
    row = Notification(
        order_id="PED-1", type="ORDER_CONFIRMATION", resend_number=0,
        recipient="cliente@example.com",
    )
    assert await NotificationPostgresAdapter(session).guardar(row) is row
    session.add.assert_called_once_with(row)
    session.flush.assert_awaited_once_with()
    session.commit.assert_not_awaited()
