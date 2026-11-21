"""Modelo phone_otp_challenge (SPEC-04).

Fuente: G3-Chatbot-specs/openspec/specs/validacion-celular.
- OTP local del chatbot, independiente de Seguridad (acuerdo A2).
- El código nunca se guarda en claro: solo su HMAC-SHA256.
- TTL de 5 min, 3 intentos y 3 envíos por ventana de 15 min (configurables).
- Solo inserción; el consumo y los intentos se actualizan en la misma fila.
- customer_id es el `sub` del token (uuid), sin FK hacia Seguridad.
- PG18: id uuid DEFAULT uuidv7().
"""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.infrastructure.db.connection import Base


class PhoneOtpChallenge(Base):
    __tablename__ = "phone_otp_challenge"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    # HMAC-SHA256(customer_id + phone + code) con PHONE_OTP_SECRET.
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(r"phone ~ '^\+51[0-9]{9}$'", name="ck_phone_otp_challenge_format"),
        CheckConstraint("attempts BETWEEN 0 AND 3", name="ck_phone_otp_challenge_attempts"),
        Index(
            "ix_phone_otp_challenge_customer_created_at",
            "customer_id",
            text("created_at DESC"),
        ),
        Index(
            "ix_phone_otp_challenge_customer_phone",
            "customer_id",
            "phone",
        ),
    )
