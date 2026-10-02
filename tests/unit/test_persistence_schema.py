"""Contrato DDL PostgreSQL de docs/modelo-datos.md (H2-06)."""

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from src.adapters.outbound.persistence.models import Base, Evidencia, Mensaje


def test_modelos_registrados_sin_inventar_tablas_de_companeros():
    assert Base.metadata.tables["mensaje"] is Mensaje.__table__
    assert Base.metadata.tables["evidencia"] is Evidencia.__table__


def test_ddl_mensaje_constraints_busqueda_y_fk(persistence_metadata):
    table = persistence_metadata.tables["mensaje"]
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert "DEFAULT uuidv7()" in ddl
    assert "TIMESTAMP WITH TIME ZONE" in ddl
    assert "REFERENCES conversacion (id) ON DELETE CASCADE" in ddl
    assert "GENERATED ALWAYS AS (to_tsvector('spanish', coalesce(texto, ''))) STORED" in ddl
    assert "rol IN ('CLIENTE','ASISTENTE','HERRAMIENTA')" in ddl
    assert "rol <> 'HERRAMIENTA' OR herramienta IS NOT NULL" in ddl
    for name in ("tokens_entrada", "tokens_salida", "latencia_ms"):
        assert f"CHECK ({name} >= 0)" in ddl
    for name in ("bloques", "argumentos"):
        assert f"jsonb_typeof({name}) = 'object'" in ddl
        assert table.c[name].type.none_as_null is True
    assert "actualizado_en" not in table.c
    indexes = "\n".join(
        str(CreateIndex(index).compile(dialect=postgresql.dialect())) for index in table.indexes
    )
    assert "(conversacion_id, creado_en DESC)" in indexes
    assert "USING gin (busqueda)" in indexes


def test_ddl_evidencia_constraints_y_fk(persistence_metadata):
    table = persistence_metadata.tables["evidencia"]
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert "REFERENCES conversacion (id) ON DELETE CASCADE" in ddl
    assert "REFERENCES devolucion_ref (devolucion_id) ON DELETE RESTRICT" in ddl
    assert "tamanio_bytes BETWEEN 1 AND 5242880" in ddl
    assert table.c.devolucion_id.nullable is True
    assert not any("tipo" in str(c.sqltext) for c in table.constraints if hasattr(c, "sqltext"))
    assert table.c.actualizado_en.server_onupdate is not None
    assert table.c.actualizado_en.onupdate is None
    indexes = "\n".join(
        str(CreateIndex(index).compile(dialect=postgresql.dialect())) for index in table.indexes
    )
    assert "(conversacion_id, creado_en) WHERE devolucion_id IS NULL" in indexes
    assert "(conversacion_id)" in indexes
    assert "(devolucion_id)" in indexes
