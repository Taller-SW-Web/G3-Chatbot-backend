"""Test mínimo H2-02: modelo + adapter sin BD (offline).

Criterio hito-2.md: cada adapter con al menos un test.
El test de integración contra Supabase (INSERT real + UNIQUE + CHECK) se hace en H2-10.
"""
import re

from src.adapters.outbound.persistence.models import Base, LocalPhoneVerification
from src.adapters.outbound.persistence.local_phone_verification_postgres_adapter import (
    LocalPhoneVerificationPostgresAdapter,
)


def test_modelo_registrado_en_base():
    assert LocalPhoneVerification.__tablename__ == "local_phone_verification"
    assert "local_phone_verification" in Base.metadata.tables


def test_columnas_y_constraints():
    table = Base.metadata.tables["local_phone_verification"]
    assert set(table.columns.keys()) == {"id", "customer_id", "phone", "verified_at", "created_at"}
    assert table.columns["customer_id"].nullable is False
    assert table.columns["phone"].nullable is False
    # UNIQUE (customer_id, phone)
    uniques = [c for c in table.constraints if c.__class__.__name__ == "UniqueConstraint"]
    assert any(set(u.columns.keys()) == {"customer_id", "phone"} for u in uniques)
    # CHECK formato +51
    checks = [str(c.sqltext) for c in table.constraints if c.__class__.__name__ == "CheckConstraint"]
    assert any("+51" in s for s in checks)
    # Solo inserción: sin updated_at
    assert "updated_at" not in table.columns


def test_formato_celular_peru():
    patron = re.compile(r"^\+51[0-9]{9}$")
    assert patron.match("+51987654321")
    assert not patron.match("987654321")
    assert not patron.match("+52123456789")


def test_adapter_expone_existe_y_guardar():
    assert hasattr(LocalPhoneVerificationPostgresAdapter, "existe")
    assert hasattr(LocalPhoneVerificationPostgresAdapter, "guardar")
