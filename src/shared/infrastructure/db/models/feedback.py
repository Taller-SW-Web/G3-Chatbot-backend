"""Modelos de retroalimentación del cliente (SPEC-24)."""
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

from src.shared.infrastructure.db.connection import Base


class MessageReaction(Base):
    """👍/👎 de un mensaje; una reacción vigente por mensaje y por quien reacciona."""

    __tablename__ = "message_reaction"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("message.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    anonymous_sid: Mapped[str | None] = mapped_column(Text, nullable=True)
    reaction: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("reaction IN ('POSITIVE','NEGATIVE')", name="ck_message_reaction_value"),
        CheckConstraint(
            "customer_id IS NOT NULL OR anonymous_sid IS NOT NULL",
            name="ck_message_reaction_owner",
        ),
        UniqueConstraint(
            "message_id",
            "customer_id",
            "anonymous_sid",
            name="uq_message_reaction_message_owner",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_message_reaction_message_id", "message_id"),
    )


class FeedbackSuggestion(Base):
    __tablename__ = "feedback_suggestion"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    contact_email: Mapped[str] = mapped_column(Text, nullable=False)
    screenshot_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    notification_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'PENDING'")
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("type IN ('SUGGESTION','BUG')", name="ck_feedback_suggestion_type"),
        CheckConstraint("char_length(title) BETWEEN 3 AND 120", name="ck_feedback_suggestion_title"),
        CheckConstraint(
            "char_length(description) BETWEEN 6 AND 1000",
            name="ck_feedback_suggestion_description",
        ),
        CheckConstraint(
            "notification_status IN ('PENDING','SENT','FAILED')",
            name="ck_feedback_suggestion_notification",
        ),
        Index("ix_feedback_suggestion_customer_created", "customer_id", text("created_at DESC")),
    )


class PurchaseRating(Base):
    __tablename__ = "purchase_rating"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    order_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_purchase_rating_value"),
        CheckConstraint("comment IS NULL OR char_length(comment) <= 500", name="ck_purchase_rating_comment"),
    )
