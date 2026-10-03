"""Modelo payment_attempt (H2-04 David, SPEC-14).

Fuente: G3-Chatbot-specs/docs/modelo-datos.md (seccion `payment_attempt`)
+ openspec/specs/checkout-pago (Req. 5: maximo 3 intentos).
- Solo insercion: sin updated_at. El intento nunca se modifica.
- Guarda solo marca + ultimos 4: NUNCA PAN, CVV ni vencimiento.
- Un APROBADO no tiene motivo; un RECHAZADO/ERROR siempre lo tiene.
- PG18: id uuid DEFAULT uuidv7().
"""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.connection import Base


class PaymentAttempt(Base):
    __tablename__ = "payment_attempt"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    checkout_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("checkout.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Generado por el simulador (SIM-...).
    transaction_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    brand: Mapped[str] = mapped_column(Text, nullable=False)
    last4: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "result IN ('APPROVED','REJECTED','ERROR')",
            name="ck_payment_attempt_result",
        ),
        CheckConstraint(
            "reason IN ('INSUFFICIENT_FUNDS','DECLINED_BY_ISSUER','PROCESSING_ERROR','INVALID_DATA')",
            name="ck_payment_attempt_reason",
        ),
        CheckConstraint(
            "brand IN ('VISA','MASTERCARD','AMEX')",
            name="ck_payment_attempt_brand",
        ),
        CheckConstraint(
            "last4 ~ '^[0-9]{4}$'", name="ck_payment_attempt_last4"
        ),
        CheckConstraint("amount > 0", name="ck_payment_attempt_amount"),
        # Aprobado sin motivo; rechazado/error siempre con motivo.
        CheckConstraint(
            "(result = 'APPROVED') = (reason IS NULL)",
            name="ck_payment_attempt_result_reason",
        ),
        # Intentos de un checkout. Cubre la FK.
        Index("ix_payment_attempt_checkout_id", "checkout_id"),
        Index("uq_payment_attempt_idempotency_key", "idempotency_key", unique=True),
    )
