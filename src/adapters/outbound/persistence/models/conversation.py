"""Modelo conversation (H2-07, SPEC-05).

Fuente: G3-Chatbot-specs/docs/modelo-datos.md (seccion `conversation`).
- Toda conversacion tiene dueno: cliente autenticado o sid anonimo.
- customer_id es el `sub` del token (uuid), sin FK hacia Seguridad (ADR-0013).
- title_search_vector es una columna generada (tsvector) con indice GIN.
- PG18: id uuid DEFAULT uuidv7().
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Computed, DateTime, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.connection import Base


class Conversation(Base):
    __tablename__ = "conversation"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    # sub del token, sin FK (ADR-0013). Null si la conversacion es anonima.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Hash de la cookie chat_sid.
    anonymous_sid: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Null mientras no hay mensajes (no aparece en el listado).
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ACTIVE'"))
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('spanish', coalesce(title, ''))", persisted=True),
        nullable=False,
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("char_length(title) <= 40", name="ck_conversation_title_length"),
        CheckConstraint(
            "status IN ('ACTIVE','ARCHIVED','CLOSED')", name="ck_conversation_status"
        ),
        CheckConstraint("jsonb_typeof(context) = 'object'", name="ck_conversation_context_object"),
        CheckConstraint(
            "customer_id IS NOT NULL OR anonymous_sid IS NOT NULL", name="ck_conversation_owner"
        ),
        # Listado paginado de la barra lateral.
        Index(
            "ix_conversation_customer_last_message",
            "customer_id",
            text("last_message_at DESC"),
            postgresql_where=text("title IS NOT NULL"),
        ),
        # Recuperar la conversacion anonima y reasignarla al iniciar sesion.
        Index(
            "ix_conversation_anonymous_sid",
            "anonymous_sid",
            postgresql_where=text("customer_id IS NULL"),
        ),
        # Job de expiracion de conversaciones anonimas (7 dias).
        Index(
            "ix_conversation_anonymous_last_message",
            "last_message_at",
            postgresql_where=text("customer_id IS NULL"),
        ),
        # Job de archivado automatico (90 dias).
        Index(
            "ix_conversation_active_last_message",
            "last_message_at",
            postgresql_where=text("status = 'ACTIVE' AND customer_id IS NOT NULL"),
        ),
        # Buscar chats por titulo.
        Index("ix_conversation_title_search_vector", "title_search_vector", postgresql_using="gin"),
    )
