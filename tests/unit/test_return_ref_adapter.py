"""Tests H2-04 (David): modelo return_ref + adapter sin BD (offline).

Trazabilidad:
- SPEC-21 (solicitud-devolucion-cambio): tipos CAMBIO/DEVOLUCION_DINERO
  guardados en ingles; sin evidence_refs (vía evidence.return_id).
- SPEC-22 (consulta-devolucion-reembolso): respaldo local, verdad en Ventas.
- docs/modelo-datos.md (seccion `return_ref`): solo insercion, sin FK a order.
"""
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models import Base, ReturnRef
from src.adapters.outbound.persistence.return_ref_postgres_adapter import (
    ReturnRefPostgresAdapter,
)

table = Base.metadata.tables["return_ref"]


def _checks():
    return [str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)]


def test_modelo_registrado_en_base():
    assert ReturnRef.__tablename__ == "return_ref"
    assert "return_ref" in Base.metadata.tables


def test_columnas_pk_text_y_solo_insercion():
    assert set(table.columns.keys()) == {
        "return_id", "code", "order_id", "customer_id", "requested_type",
        "idempotency_key", "created_at",
    }
    assert "updated_at" not in table.columns.keys()
    assert "evidence_refs" not in table.columns.keys()
    assert list(table.primary_key.columns.keys()) == ["return_id"]
    assert table.columns["return_id"].server_default is None


def test_order_id_sin_fk_otro_canal():
    assert not table.foreign_keys


def test_check_requested_type_en_ingles():
    assert any(
        "EXCHANGE" in s and "MONEY_REFUND" in s for s in _checks()
    )


def test_traduccion_ventas_sin_inventar():
    assert ReturnRefPostgresAdapter.tipo_desde_ventas("CAMBIO") == "EXCHANGE"
    assert ReturnRefPostgresAdapter.tipo_desde_ventas("DEVOLUCION_DINERO") == "MONEY_REFUND"
    assert ReturnRefPostgresAdapter.tipo_hacia_ventas("EXCHANGE") == "CAMBIO"
    assert ReturnRefPostgresAdapter.tipo_hacia_ventas("MONEY_REFUND") == "DEVOLUCION_DINERO"
    assert ReturnRefPostgresAdapter.tipo_desde_ventas("FUTURO") == "FUTURO"


def test_adapter_expone_operaciones():
    for method in (
        "guardar", "obtener_por_return_id", "obtener_por_code",
        "obtener_por_idempotency_key",
    ):
        assert hasattr(ReturnRefPostgresAdapter, method)


@pytest.fixture
def session():
    return MagicMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_guardar_traduce_a_ingles_y_hace_flush(session):
    """SPEC-21: Ventas envia CAMBIO; la base guarda EXCHANGE."""
    row = ReturnRef(
        return_id="DEV-1", code="DEV-2026-0001", order_id="PED-1",
        customer_id=uuid.uuid4(), requested_type="CAMBIO",
        idempotency_key="key-1",
    )
    assert await ReturnRefPostgresAdapter(session).guardar(row) is row
    assert row.requested_type == "EXCHANGE"
    session.add.assert_called_once_with(row)
    session.flush.assert_awaited_once_with()
    session.commit.assert_not_awaited()
