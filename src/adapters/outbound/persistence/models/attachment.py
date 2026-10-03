"""Modelo attachment (H2-07, SPEC-23, ADR-0019).

Fuente: G3-Chatbot-specs/docs/modelo-datos.md (seccion `attachment`).
- Solo referencias: los bytes viven en un bucket privado (puerto AttachmentStorage).
- Se crea PENDING sin mensaje y se liga a un unico mensaje al enviarlo (SENT).
- El nombre original del archivo no se guarda.
- El limite de 3 adjuntos por mensaje se valida en AdjuntoService, no en la BD.
- PG18: id uuid DEFAULT uuidv7().
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.connection import Base


class Attachment(Base):
    __tablename__ = "attachment"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversation.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Null mientras el estado es PENDING. FK como cadena: el modelo message se agrega en H2-06.
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("message.id", ondelete="CASCADE"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'PENDING'"))
    # Clave aleatoria del objeto en el bucket; no deriva del nombre original.
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    thumbnail_key: Mapped[str] = mapped_column(Text, nullable=False)
    # Detectado por firma binaria.
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    # created_at es la fecha de carga y la usa el job de limpieza (24 h).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_attachment_storage_key"),
        UniqueConstraint("thumbnail_key", name="uq_attachment_thumbnail_key"),
        CheckConstraint("status IN ('PENDING','SENT')", name="ck_attachment_status"),
        CheckConstraint(
            "mime_type IN ('image/jpeg','image/png','image/webp')", name="ck_attachment_mime_type"
        ),
        CheckConstraint("size_bytes BETWEEN 1 AND 5242880", name="ck_attachment_size_bytes"),
        CheckConstraint("width > 0", name="ck_attachment_width"),
        CheckConstraint("height > 0", name="ck_attachment_height"),
        CheckConstraint(
            "(status = 'SENT') = (message_id IS NOT NULL)", name="ck_attachment_status_message"
        ),
        # Adjuntos de un mensaje al armar el historial. Cubre la FK.
        Index("ix_attachment_message_id", "message_id", postgresql_where=text("message_id IS NOT NULL")),
        # Conteo de PENDING por conversacion y pertenencia. Cubre la FK.
        Index("ix_attachment_conversation_status", "conversation_id", "status"),
        # Job de limpieza de adjuntos no enviados.
        Index(
            "ix_attachment_pending_created_at",
            "created_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
    )
