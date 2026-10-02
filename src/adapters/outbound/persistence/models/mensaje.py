"""Mensaje persistido (H2-06, SPEC-05). El texto debe llegar redactado.

Solo inserción. La identidad y pertenencia se validan en el caso de uso;
las claves foráneas locales se completan con el modelo de Mathias.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Computed, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.connection import Base


class Mensaje(Base):
    __tablename__ = "mensaje"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    conversacion_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversacion.id", ondelete="CASCADE"), nullable=False
    )
    rol: Mapped[str] = mapped_column(Text, nullable=False)
    texto: Mapped[str | None] = mapped_column(Text)
    bloques: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    herramienta: Mapped[str | None] = mapped_column(Text)
    argumentos: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    resultado_codigo: Mapped[str | None] = mapped_column(Text)
    tokens_entrada: Mapped[int | None] = mapped_column(Integer)
    tokens_salida: Mapped[int | None] = mapped_column(Integer)
    latencia_ms: Mapped[int | None] = mapped_column(Integer)
    busqueda: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('spanish', coalesce(texto, ''))", persisted=True),
        nullable=False,
    )
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("rol IN ('CLIENTE','ASISTENTE','HERRAMIENTA')", name="ck_mensaje_rol"),
        CheckConstraint(
            "rol <> 'HERRAMIENTA' OR herramienta IS NOT NULL", name="ck_mensaje_herramienta"
        ),
        CheckConstraint("jsonb_typeof(bloques) = 'object'", name="ck_mensaje_bloques_objeto"),
        CheckConstraint("jsonb_typeof(argumentos) = 'object'", name="ck_mensaje_argumentos_objeto"),
        CheckConstraint("tokens_entrada >= 0", name="ck_mensaje_tokens_entrada"),
        CheckConstraint("tokens_salida >= 0", name="ck_mensaje_tokens_salida"),
        CheckConstraint("latencia_ms >= 0", name="ck_mensaje_latencia"),
        Index("ix_mensaje_conversacion_creado", "conversacion_id", creado_en.desc()),
        Index("ix_mensaje_busqueda", "busqueda", postgresql_using="gin"),
    )
