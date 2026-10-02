"""Modelo celular_verificacion_local (H2-02, SPEC-04).

Fuente: G3-Chatbot-specs/docs/modelo-datos.md:108-121 + SPEC-04 validacion-celular.
- Verificación propia del chatbot, independiente de Seguridad (acuerdo A2).
- Solo inserción: sin actualizado_en, sin trigger, sin FK externa (ADR-0013).
- cliente_id es el `sub` del token (uuid), sin FK hacia Seguridad.
- PG18: id uuid DEFAULT uuidv7().
"""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.connection import Base


class CelularVerificacionLocal(Base):
    __tablename__ = "celular_verificacion_local"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    # sub del token, sin FK (ADR-0013: solo referencias locales).
    cliente_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # CHECK (celular ~ '^\+51[0-9]{9}$'). Número vigente en GET /auth/me al verificar.
    celular: Mapped[str] = mapped_column(Text, nullable=False)
    verificado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("cliente_id", "celular", name="uq_celular_verificacion_cliente_celular"),
        CheckConstraint(r"celular ~ '^\+51[0-9]{9}$'", name="ck_celular_verificacion_formato"),
    )
