"""Tests H2-04 (David): modelo claim_ref + adapter sin BD (offline).

Trazabilidad:
- SPEC-19 (creacion-reclamo): idempotencia por Idempotency-Key.
- SPEC-20 (consulta-reclamo): pertenencia por customer_id; el listado
  vive en Ventas, aqui solo la referencia.
- docs/modelo-datos.md (seccion `claim_ref`): solo insercion, sin FK a order.
"""
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models import Base, ClaimRef
from src.adapters.outbound.persistence.claim_ref_postgres_adapter import (
    ClaimRefPostgresAdapter,
)

table = Base.metadata.tables["claim_ref"]


def test_modelo_registrado_en_base():
    assert ClaimRef.__tablename__ == "claim_ref"
    assert "claim_ref" in Base.metadata.tables


def test_columnas_pk_text_y_solo_insercion():
    assert set(table.columns.keys()) == {
        "claim_id", "code", "order_id", "customer_id",
        "idempotency_key", "created_at",
    }
    assert "updated_at" not in table.columns.keys()
    assert list(table.primary_key.columns.keys()) == ["claim_id"]
    # IDs de Ventas: text sin default uuid.
    assert table.columns["claim_id"].server_default is None
    for required in (
        "claim_id", "code", "order_id", "customer_id",
        "idempotency_key", "created_at",
    ):
        assert table.columns[required].nullable is False


def test_uniques_code_e_idempotency():
    assert table.columns["code"].unique is True
    assert table.columns["idempotency_key"].unique is True


def test_order_id_sin_fk_otro_canal():
    # El pedido puede venir de otro canal: sin fila en order_ref.
    assert "order_id" not in {fk.parent.name for fk in table.foreign_keys}
    assert not table.foreign_keys


def test_adapter_expone_operaciones():
    for method in (
        "guardar", "obtener_por_claim_id", "obtener_por_code",
        "obtener_por_idempotency_key",
    ):
        assert hasattr(ClaimRefPostgresAdapter, method)


@pytest.fixture
def session():
    return MagicMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_guardar_idempotente_hace_flush_sin_commit(session):
    """SPEC-19: reintento con la misma clave no duplica el reclamo."""
    row = ClaimRef(
        claim_id="REC-1", code="RCL-0001", order_id="PED-1",
        customer_id=uuid.uuid4(), idempotency_key="key-1",
    )
    assert await ClaimRefPostgresAdapter(session).guardar(row) is row
    session.add.assert_called_once_with(row)
    session.flush.assert_awaited_once_with()
    session.commit.assert_not_awaited()
