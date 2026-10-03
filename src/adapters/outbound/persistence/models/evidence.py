"""Referencia local a evidencia alojada por Ventas (H2-06, SPEC-21).

La URL no se envía al LLM. H2-10 debe instalar set_updated_at();
server_onupdate declara ese valor generado, no crea el trigger.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, FetchedValue, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.connection import Base


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    return_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("return_ref.return_id", ondelete="RESTRICT")
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    original_file_name: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
        server_onupdate=FetchedValue(),
    )

    __table_args__ = (
        CheckConstraint(
            "size_bytes BETWEEN 1 AND 5242880", name="ck_evidence_size_bytes"
        ),
        Index(
            "ix_evidence_draft", "conversation_id", "created_at",
            postgresql_where=text("return_id IS NULL"),
        ),
        Index("ix_evidence_conversation_id", "conversation_id"),
        Index("ix_evidence_return_id", "return_id"),
    )
