"""Modelo outbox (H2-03, SPEC-15, SPEC-16).

Fuente: G3-Chatbot-specs/docs/modelo-datos.md (seccion `outbox`) + ADR-0011.
- Patron transaccional: la fila se escribe en la misma transaccion que el
  intento de pago / checkout, y el worker de APScheduler la procesa con
  reintento (backoff 5s, 15s, 45s, 2min, 5min, maximo 5 intentos).
- Tipos: NOTIFY_SALES_PAYMENT (avisa a Ventas del pago aprobado),
  REQUEST_CANCELLATION (anula no pagados) y SEND_EMAIL (confirmacion).
- Polling del worker: WHERE status = 'PENDING' AND next_attempt_at <= now()
  ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED.
- H2-10 debe instalar set_updated_at(); server_onupdate declara ese valor
  generado, no crea el trigger.
- PG18: id uuid DEFAULT uuidv7().
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, FetchedValue, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.connection import Base


class Outbox(Base):
    __tablename__ = "outbox"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'PENDING'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
        server_onupdate=FetchedValue(),
    )

    __table_args__ = (
        CheckConstraint(
            "type IN ('NOTIFY_SALES_PAYMENT','REQUEST_CANCELLATION','SEND_EMAIL')",
            name="ck_outbox_type",
        ),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_outbox_payload_object"),
        CheckConstraint(
            "status IN ('PENDING','PROCESSED','FAILED')", name="ck_outbox_status"
        ),
        CheckConstraint("attempts BETWEEN 0 AND 5", name="ck_outbox_attempts"),
        # Polling del worker con SKIP LOCKED (permite mas de un worker).
        Index(
            "ix_outbox_next_attempt_at",
            "next_attempt_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
        # Vista de administracion /admin/outbox-fallidos.
        Index(
            "ix_outbox_failed_created_at",
            text("created_at DESC"),
            postgresql_where=text("status = 'FAILED'"),
        ),
    )
