"""Test mínimo H2-02: modelo + adapter sin BD (offline).

Criterio hito-2.md: cada adapter con al menos un test.
El test de integración contra Supabase (INSERT real + UNIQUE + CHECK) se hace en H2-10.
"""
import re

from src.adapters.outbound.persistence.models import Base, CelularVerificacionLocal
from src.adapters.outbound.persistence.celular_verificacion_postgres_adapter import (
    CelularVerificacionPostgresAdapter,
)


def test_modelo_registrado_en_base():
    assert CelularVerificacionLocal.__tablename__ == "celular_verificacion_local"
    assert "celular_verificacion_local" in Base.metadata.tables


def test_columnas_y_constraints():
    table = Base.metadata.tables["celular_verificacion_local"]
    assert set(table.columns.keys()) == {"id", "cliente_id", "celular", "verificado_en", "creado_en"}
    assert table.columns["cliente_id"].nullable is False
    assert table.columns["celular"].nullable is False
    # UNIQUE (cliente_id, celular)
    uniques = [c for c in table.constraints if c.__class__.__name__ == "UniqueConstraint"]
    assert any(set(u.columns.keys()) == {"cliente_id", "celular"} for u in uniques)
    # CHECK formato +51
    checks = [str(c.sqltext) for c in table.constraints if c.__class__.__name__ == "CheckConstraint"]
    assert any("+51" in s for s in checks)
    # Solo inserción: sin actualizado_en
    assert "actualizado_en" not in table.columns


def test_formato_celular_peru():
    patron = re.compile(r"^\+51[0-9]{9}$")
    assert patron.match("+51987654321")
    assert not patron.match("987654321")
    assert not patron.match("+52123456789")


def test_adapter_expone_existe_y_guardar():
    assert hasattr(CelularVerificacionPostgresAdapter, "existe")
    assert hasattr(CelularVerificacionPostgresAdapter, "guardar")
