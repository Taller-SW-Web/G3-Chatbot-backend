"""Contrato DDL PostgreSQL de docs/modelo-datos.md (H2-06)."""

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from src.adapters.outbound.persistence.models import Attachment, Base, Conversation, Evidence, Message


def test_modelos_registrados_sin_inventar_tablas_de_companeros():
    assert Base.metadata.tables["message"] is Message.__table__
    assert Base.metadata.tables["evidence"] is Evidence.__table__


def test_fk_conversation_message_attachment_resuelven_modelos_reales(persistence_metadata):
    assert next(iter(Message.__table__.c.conversation_id.foreign_keys)).column is Conversation.__table__.c.id
    assert next(iter(Attachment.__table__.c.message_id.foreign_keys)).column is Message.__table__.c.id
    assert {t.name for t in persistence_metadata.sorted_tables} == {
        "conversation", "message", "attachment", "evidence", "return_ref",
    }


def test_ddl_mensaje_constraints_busqueda_y_fk(persistence_metadata):
    table = persistence_metadata.tables["message"]
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert "DEFAULT uuidv7()" in ddl
    assert "TIMESTAMP WITH TIME ZONE" in ddl
    assert "REFERENCES conversation (id) ON DELETE CASCADE" in ddl
    assert "GENERATED ALWAYS AS (to_tsvector('spanish', coalesce(content, ''))) STORED" in ddl
    assert "role IN ('CUSTOMER','ASSISTANT','TOOL')" in ddl
    assert "role <> 'TOOL' OR tool_name IS NOT NULL" in ddl
    for name in ("input_tokens", "output_tokens", "latency_ms"):
        assert f"CHECK ({name} >= 0)" in ddl
    for name in ("blocks", "arguments"):
        assert f"jsonb_typeof({name}) = 'object'" in ddl
        assert table.c[name].type.none_as_null is True
    assert "updated_at" not in table.c
    indexes = "\n".join(
        str(CreateIndex(index).compile(dialect=postgresql.dialect())) for index in table.indexes
    )
    assert "(conversation_id, created_at DESC)" in indexes
    assert "USING gin (search_vector)" in indexes


def test_ddl_evidencia_constraints_y_fk(persistence_metadata):
    table = persistence_metadata.tables["evidence"]
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert "REFERENCES conversation (id) ON DELETE CASCADE" in ddl
    assert "REFERENCES return_ref (return_id) ON DELETE RESTRICT" in ddl
    assert "size_bytes BETWEEN 1 AND 5242880" in ddl
    assert table.c.return_id.nullable is True
    assert not any("type" in str(c.sqltext) for c in table.constraints if hasattr(c, "sqltext"))
    assert table.c.updated_at.server_onupdate is not None
    assert table.c.updated_at.onupdate is None
    indexes = "\n".join(
        str(CreateIndex(index).compile(dialect=postgresql.dialect())) for index in table.indexes
    )
    assert "(conversation_id, created_at) WHERE return_id IS NULL" in indexes
    assert "(conversation_id)" in indexes
    assert "(return_id)" in indexes
