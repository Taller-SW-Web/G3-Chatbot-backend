"""Tests H2-03 (Sonny): modelo order_ref y adapter sin BD (offline).

checkout.id aun no esta definido (H2-04 David): estos tests solo
inspeccionan la tabla y no resuelven esa FK (patron de H2-07 con
attachment.message_id antes de H2-06).
"""
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models import Base, OrderRef
from src.adapters.outbound.persistence.order_ref_postgres_adapter import OrderRefPostgresAdapter

table = Base.metadata.tables["order_ref"]


def _checks():
    return [str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)]


def test_modelo_registrado_en_base():
    assert OrderRef.__tablename__ == "order_ref"
    assert "order_ref" in Base.metadata.tables


def test_columnas_y_nulabilidad():
    assert set(table.columns.keys()) == {
        "order_id", "customer_id", "checkout_id", "local_status",
        "total", "created_at", "updated_at",
    }
    for required in ("order_id", "customer_id", "checkout_id", "local_status",
                     "total", "created_at", "updated_at"):
        assert table.columns[required].nullable is False
    # order_id es el ID de Ventas (text), no un uuid generado.
    assert table.columns["order_id"].server_default is None
    assert "CREATED" in str(table.columns["local_status"].server_default.arg)
    assert table.columns["updated_at"].server_onupdate is not None


def test_pk_y_unique_checkout():
    assert list(table.primary_key.columns.keys()) == ["order_id"]
    uniques = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
    assert {u.name for u in uniques} == {"uq_order_ref_checkout_id"}
    assert {i.name for i in table.indexes} >= {"ix_order_ref_checkout_id", "ix_order_ref_customer_id"}


def test_fk_checkout_sin_resolver():
    # H2-04 creara el modelo checkout; aqui solo se verifica el nombre y el RESTRICT.
    fks = {fk.parent.name: fk for fk in table.foreign_keys}
    assert set(fks) == {"checkout_id"}
    assert fks["checkout_id"].target_fullname == "checkout.id"
    assert fks["checkout_id"].ondelete == "RESTRICT"


def test_check_constraints():
    checks = _checks()
    assert any("CREATED" in s and "CANCELLED" in s for s in checks)
    assert any("total >= 0" in s for s in checks)


def test_traduccion_anulado_no_inventa_literales():
    assert OrderRefPostgresAdapter.estado_desde_ventas("ANULADO") == "CANCELLED"
    assert OrderRefPostgresAdapter.estado_hacia_ventas("CANCELLED") == "ANULADO"
    assert OrderRefPostgresAdapter.estado_desde_ventas("PAGADO") == "PAGADO"


def test_adapter_expone_operaciones():
    for method in ("guardar", "obtener_por_order_id", "obtener_por_checkout_id", "actualizar_estado"):
        assert hasattr(OrderRefPostgresAdapter, method)


@pytest.fixture
def session():
    return MagicMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_guardar_normaliza_anulado_y_hace_flush_sin_commit(session):
    """SPEC-15: Ventas responde ANULADO; la base guarda CANCELLED."""
    row = OrderRef(order_id="PED-1", customer_id=uuid.uuid4(),
                   checkout_id=uuid.uuid4(), local_status="ANULADO", total=100)
    assert await OrderRefPostgresAdapter(session).guardar(row) is row
    assert row.local_status == "CANCELLED"
    session.add.assert_called_once_with(row)
    session.flush.assert_awaited_once_with()
    session.commit.assert_not_awaited()
