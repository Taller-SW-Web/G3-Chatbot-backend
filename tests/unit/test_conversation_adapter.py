"""Tests H2-07: modelo conversation + adapter sin BD (offline).

Criterio hito-2.md: cada adapter con al menos un test.
El test de integración contra Supabase (INSERT real + CHECKs + índices) se hace en H2-10.
"""
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID

from src.adapters.outbound.persistence.models import Base, Conversation
from src.adapters.outbound.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)

table = Base.metadata.tables["conversation"]


def _checks():
    return [str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)]


def _index(name):
    return next(i for i in table.indexes if i.name == name)


def test_modelo_registrado_en_base():
    assert Conversation.__tablename__ == "conversation"
    assert "conversation" in Base.metadata.tables


def test_columnas_y_nulabilidad():
    assert set(table.columns.keys()) == {
        "id", "customer_id", "anonymous_sid", "title", "status", "context", "summary",
        "title_search_vector", "last_message_at", "created_at", "updated_at",
    }
    for nullable in ("customer_id", "anonymous_sid", "title", "summary"):
        assert table.columns[nullable].nullable is True
    for required in ("status", "context", "title_search_vector", "last_message_at", "created_at", "updated_at"):
        assert table.columns[required].nullable is False
    assert isinstance(table.columns["id"].type, UUID)
    assert isinstance(table.columns["context"].type, JSONB)
    assert table.columns["last_message_at"].type.timezone is True


def test_pk_y_defaults():
    assert list(table.primary_key.columns.keys()) == ["id"]
    assert "uuidv7()" in str(table.columns["id"].server_default.arg)
    assert "ACTIVE" in str(table.columns["status"].server_default.arg)
    assert "{}" in str(table.columns["context"].server_default.arg)
    for col in ("last_message_at", "created_at", "updated_at"):
        assert "now()" in str(table.columns[col].server_default.arg)


def test_sin_fk_externas():
    assert not table.foreign_keys


def test_check_constraints():
    checks = _checks()
    assert any("char_length(title) <= 40" in s for s in checks)
    assert any("ACTIVE" in s and "ARCHIVED" in s and "CLOSED" in s for s in checks)
    assert any("jsonb_typeof(context)" in s and "object" in s for s in checks)
    assert any("customer_id IS NOT NULL OR anonymous_sid IS NOT NULL" in s for s in checks)


def test_title_search_vector_es_columna_generada():
    col = table.columns["title_search_vector"]
    assert isinstance(col.type, TSVECTOR)
    assert col.computed is not None
    assert col.computed.persisted is True
    expr = str(col.computed.sqltext)
    assert "to_tsvector('spanish'" in expr and "coalesce(title" in expr


def test_indices_parciales():
    assert str(_index("ix_conversation_customer_last_message").dialect_options["postgresql"]["where"]) == "title IS NOT NULL"
    assert str(_index("ix_conversation_anonymous_sid").dialect_options["postgresql"]["where"]) == "customer_id IS NULL"
    assert str(_index("ix_conversation_anonymous_last_message").dialect_options["postgresql"]["where"]) == "customer_id IS NULL"
    assert (
        str(_index("ix_conversation_active_last_message").dialect_options["postgresql"]["where"])
        == "status = 'ACTIVE' AND customer_id IS NOT NULL"
    )


def test_indice_listado_ordena_desc():
    idx = _index("ix_conversation_customer_last_message")
    assert "DESC" in " ".join(str(e) for e in idx.expressions)
    assert idx.columns.keys() == ["customer_id"] or "customer_id" in idx.columns.keys()


def test_indice_gin_title_search_vector():
    idx = _index("ix_conversation_title_search_vector")
    assert idx.dialect_options["postgresql"]["using"] == "gin"
    assert idx.columns.keys() == ["title_search_vector"]


def test_adapter_expone_operaciones():
    for method in ("guardar", "obtener_por_id", "listar_por_cliente", "obtener_anonima_por_sid"):
        assert hasattr(ConversationPostgresAdapter, method)
