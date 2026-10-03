"""Tests H2-04 (David): modelo payment_attempt + adapter sin BD (offline).

Trazabilidad:
- SPEC-14 (checkout-pago) Req. 5: maximo 3 intentos; APROBADO sin motivo,
  RECHAZADO/ERROR siempre con motivo; nunca PAN/CVV/vencimiento.
- docs/modelo-datos.md (seccion `payment_attempt`): solo insercion.
"""
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.models import Base, PaymentAttempt
from src.adapters.outbound.persistence.payment_attempt_postgres_adapter import (
    PaymentAttemptPostgresAdapter,
)

table = Base.metadata.tables["payment_attempt"]


def _checks():
    return [str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)]


def test_modelo_registrado_en_base():
    assert PaymentAttempt.__tablename__ == "payment_attempt"
    assert "payment_attempt" in Base.metadata.tables


def test_columnas_y_solo_insercion():
    assert set(table.columns.keys()) == {
        "id", "checkout_id", "idempotency_key", "result", "reason",
        "transaction_id", "brand", "last4", "amount", "created_at",
    }
    assert "updated_at" not in table.columns.keys()
    for required in (
        "checkout_id", "idempotency_key", "result", "brand", "last4",
        "amount", "created_at",
    ):
        assert table.columns[required].nullable is False
    for optional in ("reason", "transaction_id"):
        assert table.columns[optional].nullable is True


def test_sin_pan_cvv_vencimiento():
    cols = set(table.columns.keys())
    assert not ({"pan", "cvv", "card_number", "expiry", "vencimiento"} & cols)


def test_fk_checkout_restrict():
    fks = {fk.parent.name: fk for fk in table.foreign_keys}
    assert fks["checkout_id"].target_fullname == "checkout.id"
    assert fks["checkout_id"].ondelete == "RESTRICT"


def test_check_result_reason_brand_last4_amount():
    checks = _checks()
    assert any("APPROVED" in s and "REJECTED" in s for s in checks)
    assert any("INSUFFICIENT_FUNDS" in s and "INVALID_DATA" in s for s in checks)
    assert any("VISA" in s and "AMEX" in s for s in checks)
    assert any("last4" in s and "0-9" in s for s in checks)
    assert any("amount > 0" in s for s in checks)
    assert any("(result = 'APPROVED') = (reason IS NULL)" in s for s in checks)


def test_adapter_expone_operaciones():
    for method in ("guardar", "listar_por_checkout", "contar_por_checkout"):
        assert hasattr(PaymentAttemptPostgresAdapter, method)


@pytest.fixture
def session():
    return MagicMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_guardar_hace_flush_sin_commit(session):
    """SPEC-14 Req. 5: cada intento se persiste con marca + ultimos 4."""
    row = PaymentAttempt(
        checkout_id=uuid.uuid4(), idempotency_key="pay-1",
        result="APPROVED", brand="VISA", last4="1111", amount=100,
    )
    assert await PaymentAttemptPostgresAdapter(session).guardar(row) is row
    session.add.assert_called_once_with(row)
    session.flush.assert_awaited_once_with()
    session.commit.assert_not_awaited()
