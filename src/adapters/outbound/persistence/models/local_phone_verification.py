"""Modelo local_phone_verification (H2-02, SPEC-04).

Fuente: G3-Chatbot-specs/docs/modelo-datos.md (seccion `local_phone_verification`) + SPEC-04 validacion-celular.
- Verificación propia del chatbot, independiente de Seguridad (acuerdo A2).
- Solo inserción: sin updated_at, sin trigger, sin FK externa (ADR-0013).
- customer_id es el `sub` del token (uuid), sin FK hacia Seguridad.
- PG18: id uuid DEFAULT uuidv7().
"""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.connection import Base


class LocalPhoneVerification(Base):
    __tablename__ = "local_phone_verification"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    # sub del token, sin FK (ADR-0013: solo referencias locales).
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # CHECK (phone ~ '^\+51[0-9]{9}$'). Número vigente en GET /auth/me al verificar.
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("customer_id", "phone", name="uq_local_phone_verification_customer_phone"),
        CheckConstraint(r"phone ~ '^\+51[0-9]{9}$'", name="ck_local_phone_verification_format"),
    )
