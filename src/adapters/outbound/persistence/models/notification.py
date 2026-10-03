"""Modelo notification (H2-04 David, SPEC-16).

Fuente: G3-Chatbot-specs/docs/modelo-datos.md (seccion `notification`)
+ openspec/specs/notificacion-confirmacion (maximo 2 reenvios).
- order_id es FK al pedido local (ID de Ventas). La verdad del envio
  vive en el outbox (tarea SEND_EMAIL de Sonny); aqui el registro.
- type se deduce de resend_number: 0 = ORDER_CONFIRMATION, 1-2 = RESEND.
- attempts cuenta reintentos SMTP de ESTA notificacion, no reenvios.
- Unicidad (order_id, resend_number): el mismo envio no se registra dos veces.
- H2-10 debe instalar set_updated_at(); server_onupdate declara ese valor
  generado, no crea el trigger.
- PG18: id uuid DEFAULT uuidv7().
"""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, FetchedValue, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.connection import Base


class Notification(Base):
    __tablename__ = "notification"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    order_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("order_ref.order_id", ondelete="RESTRICT"),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    # 0 para ORDER_CONFIRMATION; 1 o 2 para CONFIRMATION_RESEND.
    resend_number: Mapped[int] = mapped_column(Integer, nullable=False)
    recipient: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'PENDING'")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
        server_onupdate=FetchedValue(),
    )

    __table_args__ = (
        UniqueConstraint("order_id", "resend_number", name="uq_notification_order_resend"),
        CheckConstraint(
            "type IN ('ORDER_CONFIRMATION','CONFIRMATION_RESEND')",
            name="ck_notification_type",
        ),
        CheckConstraint(
            "(type = 'ORDER_CONFIRMATION' AND resend_number = 0) OR "
            "(type = 'CONFIRMATION_RESEND' AND resend_number IN (1, 2))",
            name="ck_notification_type_resend",
        ),
        CheckConstraint(
            "status IN ('PENDING','SENT','FAILED')",
            name="ck_notification_status",
        ),
        CheckConstraint("attempts BETWEEN 0 AND 3", name="ck_notification_attempts"),
        # Idempotencia + FK: el mismo envio no se registra dos veces.
        Index("ix_notification_order_id", "order_id"),
    )
