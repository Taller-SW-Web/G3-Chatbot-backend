"""Referencia local a evidencia alojada por Ventas (H2-06, SPEC-21).

La URL no se envía al LLM. H2-10 debe instalar set_actualizado_en();
server_onupdate declara ese valor generado, no crea el trigger.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, FetchedValue, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.connection import Base


class Evidencia(Base):
    __tablename__ = "evidencia"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    devolucion_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("devolucion_ref.devolucion_id", ondelete="RESTRICT")
    )
    conversacion_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversacion.id", ondelete="CASCADE"), nullable=False
    )
    tipo: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    nombre_archivo_original: Mapped[str] = mapped_column(Text, nullable=False)
    tamanio_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
        server_onupdate=FetchedValue(),
    )

    __table_args__ = (
        CheckConstraint(
            "tamanio_bytes BETWEEN 1 AND 5242880", name="ck_evidencia_tamanio"
        ),
        Index(
            "ix_evidencia_borrador", "conversacion_id", "creado_en",
            postgresql_where=text("devolucion_id IS NULL"),
        ),
        Index("ix_evidencia_conversacion", "conversacion_id"),
        Index("ix_evidencia_devolucion", "devolucion_id"),
    )
