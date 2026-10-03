"""Mensaje persistido (H2-06, SPEC-05). El texto debe llegar redactado.

Solo inserción. La identidad y pertenencia se validan en el caso de uso;
conversation_id referencia el modelo Conversation de Mathias.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Computed, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.connection import Base


class Message(Base):
    __tablename__ = "message"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    blocks: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    tool_name: Mapped[str | None] = mapped_column(Text)
    arguments: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    result_code: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('spanish', coalesce(content, ''))", persisted=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("role IN ('CUSTOMER','ASSISTANT','TOOL')", name="ck_message_role"),
        CheckConstraint(
            "role <> 'TOOL' OR tool_name IS NOT NULL", name="ck_message_tool_name"
        ),
        CheckConstraint("jsonb_typeof(blocks) = 'object'", name="ck_message_blocks_object"),
        CheckConstraint("jsonb_typeof(arguments) = 'object'", name="ck_message_arguments_object"),
        CheckConstraint("input_tokens >= 0", name="ck_message_input_tokens"),
        CheckConstraint("output_tokens >= 0", name="ck_message_output_tokens"),
        CheckConstraint("latency_ms >= 0", name="ck_message_latency"),
        Index("ix_message_conversation_created_at", "conversation_id", created_at.desc()),
        Index("ix_message_search_vector", "search_vector", postgresql_using="gin"),
    )
