"""Tests H2-04 (David): modelo checkout + adapter sin BD (offline).

Trazabilidad:
- SPEC-12 (direccion-cotizacion-envio): summary con contacto + envio.
- SPEC-14 (checkout-pago) Req. 1, 2, 6: precondiciones, creacion con
  Idempotency-Key, vigencia 15 min, un vivo por carrito.
- docs/modelo-datos.md (seccion `checkout`): columnas, CHECKs, UQ parcial.
"""
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models import Base, Checkout
from src.adapters.outbound.persistence.checkout_postgres_adapter import CheckoutPostgresAdapter

table = Base.metadata.tables["checkout"]


def _checks():
    return [str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)]


def _index(name):
    return next(i for i in table.indexes if i.name == name)


def test_modelo_registrado_en_base():
    assert Checkout.__tablename__ == "checkout"
    assert "checkout" in Base.metadata.tables


def test_columnas_y_nulabilidad():
    assert set(table.columns.keys()) == {
        "id", "cart_id", "customer_id", "status", "summary", "total",
        "payment_attempts", "expires_at", "idempotency_key",
        "created_at", "updated_at",
    }
    for required in (
        "cart_id", "customer_id", "status", "summary", "total",
        "payment_attempts", "expires_at", "idempotency_key",
        "created_at", "updated_at",
    ):
        assert table.columns[required].nullable is False
    assert isinstance(table.columns["id"].type, UUID)
    assert isinstance(table.columns["summary"].type, JSONB)
    assert "PENDING_PAYMENT" in str(table.columns["status"].server_default.arg)
    assert "0" in str(table.columns["payment_attempts"].server_default.arg)
    assert table.columns["updated_at"].server_onupdate is not None


def test_pk_y_fk_cart():
    assert list(table.primary_key.columns.keys()) == ["id"]
    fks = {fk.parent.name: fk for fk in table.foreign_keys}
    assert fks["cart_id"].target_fullname == "cart.id"
    assert fks["cart_id"].ondelete == "RESTRICT"


def test_sin_order_id_doble_fuente():
    # La relacion vive solo en order_ref.checkout_id.
    assert "order_id" not in table.columns.keys()


def test_check_constraints():
    checks = _checks()
    assert any("PENDING_PAYMENT" in s and "EXPIRED" in s for s in checks)
    assert any("jsonb_typeof(summary)" in s and "object" in s for s in checks)
    assert any("total >= 0" in s for s in checks)
    assert any("payment_attempts BETWEEN 0 AND 3" in s for s in checks)


def test_uq_parcial_un_vivo_por_carrito():
    idx = _index("uq_checkout_cart_alive")
    assert idx.unique is True
    assert "cart_id" in idx.columns.keys()
    assert "PENDING_PAYMENT" in str(idx.dialect_options["postgresql"]["where"])


def test_indices_fk_historial_y_expiracion():
    assert "ix_checkout_cart_id" in {i.name for i in table.indexes}
    exp = _index("ix_checkout_expires_at")
    assert "PENDING_PAYMENT" in str(exp.dialect_options["postgresql"]["where"])


def test_adapter_expone_operaciones():
    for method in (
        "guardar", "obtener_por_id", "obtener_vivo_por_cart",
        "marcar_estado", "incrementar_intentos",
    ):
        assert hasattr(CheckoutPostgresAdapter, method)


@pytest.fixture
def session():
    return MagicMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_guardar_hace_flush_sin_commit(session):
    """SPEC-14 Req. 2: POST /checkout crea PENDING_PAYMENT con Idempotency-Key."""
    row = Checkout(
        cart_id=uuid.uuid4(), customer_id=uuid.uuid4(),
        summary={"contacto": {"tipoDocumento": "DNI"}}, total=100,
        expires_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        idempotency_key="key-1",
    )
    assert await CheckoutPostgresAdapter(session).guardar(row) is row
    session.add.assert_called_once_with(row)
    session.flush.assert_awaited_once_with()
    session.commit.assert_not_awaited()
