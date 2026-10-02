"""Tests H2-07: modelo attachment + adapter sin BD (offline).

message.id aun no esta definido (H2-06): estos tests solo inspeccionan la tabla y no
resuelven esa FK (sin create_all ni configuracion de mappers).
El test de integración contra Supabase se hace en H2-10.
"""
from sqlalchemy import CheckConstraint, UniqueConstraint

from src.adapters.outbound.persistence.models import Attachment, Base
from src.adapters.outbound.persistence.attachment_postgres_adapter import AttachmentPostgresAdapter

table = Base.metadata.tables["attachment"]


def _checks():
    return [str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)]


def _index(name):
    return next(i for i in table.indexes if i.name == name)


def test_modelo_registrado_en_base():
    assert Attachment.__tablename__ == "attachment"
    assert "attachment" in Base.metadata.tables


def test_columnas_y_nulabilidad():
    assert set(table.columns.keys()) == {
        "id", "conversation_id", "message_id", "status", "storage_key", "thumbnail_key",
        "mime_type", "size_bytes", "width", "height", "created_at", "updated_at",
    }
    assert table.columns["message_id"].nullable is True
    for required in (
        "conversation_id", "status", "storage_key", "thumbnail_key", "mime_type",
        "size_bytes", "width", "height", "created_at", "updated_at",
    ):
        assert table.columns[required].nullable is False
    # El nombre original del archivo nunca se guarda.
    assert not {"name", "filename", "original_file_name"} & set(table.columns.keys())


def test_pk_y_defaults():
    assert list(table.primary_key.columns.keys()) == ["id"]
    assert "uuidv7()" in str(table.columns["id"].server_default.arg)
    assert "PENDING" in str(table.columns["status"].server_default.arg)
    for col in ("created_at", "updated_at"):
        assert table.columns[col].type.timezone is True
        assert "now()" in str(table.columns[col].server_default.arg)


def test_foreign_keys_sin_resolver_message():
    fks = {fk.parent.name: fk for fk in table.foreign_keys}
    assert set(fks) == {"conversation_id", "message_id"}
    assert fks["conversation_id"].target_fullname == "conversation.id"
    assert fks["conversation_id"].ondelete == "CASCADE"
    assert fks["message_id"].target_fullname == "message.id"
    assert fks["message_id"].ondelete == "CASCADE"


def test_unique_constraints():
    uniques = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
    assert {frozenset(u.columns.keys()) for u in uniques} == {
        frozenset({"storage_key"}),
        frozenset({"thumbnail_key"}),
    }
    assert {u.name for u in uniques} == {"uq_attachment_storage_key", "uq_attachment_thumbnail_key"}


def test_check_constraints():
    checks = _checks()
    assert any("PENDING" in s and "SENT" in s and "IN" in s for s in checks)
    assert any("image/jpeg" in s and "image/png" in s and "image/webp" in s for s in checks)
    assert any("size_bytes BETWEEN 1 AND 5242880" in s for s in checks)
    assert any("width > 0" in s for s in checks)
    assert any("height > 0" in s for s in checks)
    assert any("(status = 'SENT') = (message_id IS NOT NULL)" in s for s in checks)


def test_sin_limite_de_tres_por_mensaje_en_bd():
    # El tope de 3 adjuntos por mensaje se valida en AdjuntoService, no en la BD.
    assert not any(" 3" in s for s in _checks())


def test_indices():
    assert str(_index("ix_attachment_message_id").dialect_options["postgresql"]["where"]) == "message_id IS NOT NULL"
    assert _index("ix_attachment_conversation_status").columns.keys() == ["conversation_id", "status"]
    pending = _index("ix_attachment_pending_created_at")
    assert pending.columns.keys() == ["created_at"]
    assert str(pending.dialect_options["postgresql"]["where"]) == "status = 'PENDING'"


def test_adapter_expone_operaciones():
    for method in (
        "guardar", "obtener_por_id", "listar_por_mensaje",
        "vincular_a_mensaje", "listar_pendientes_antes_de",
    ):
        assert hasattr(AttachmentPostgresAdapter, method)
