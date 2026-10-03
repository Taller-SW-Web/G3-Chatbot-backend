"""Modelo return_ref (H2-04 David, SPEC-21, SPEC-22).

Fuente: G3-Chatbot-specs/docs/modelo-datos.md (seccion `return_ref`).
- Referencia local a la devolucion/cambio registrada en Ventas (F3).
  La verdad vive en Ventas; aqui idempotencia + pertenencia al cliente.
- return_id es el ID de Ventas (text) y es la PK. code es el codigo
  visible (DEV-..., formato de Ventas, UNIQUE).
- order_id es el ID de Ventas SIN FK: el pedido puede venir de otro canal.
- requested_type se guarda en ingles; el adapter traduce en ambos
  sentidos el literal del contrato con Ventas (EXCHANGE <-> CAMBIO,
  MONEY_REFUND <-> DEVOLUCION_DINERO).
- Sin evidence_refs jsonb: las evidencias se obtienen con
  WHERE return_id = ? sobre evidence (tabla de Alonso, H2-06).
- Solo insercion: sin updated_at.
"""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.connection import Base


class ReturnRef(Base):
    __tablename__ = "return_ref"

    # PK con el ID de Ventas (text, formato del dueno).
    return_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # Codigo visible para el cliente (formato de Ventas).
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # ID de Ventas, sin FK (puede ser de otro canal).
    order_id: Mapped[str] = mapped_column(Text, nullable=False)
    # sub del token, sin FK (ADR-0013).
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    requested_type: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "requested_type IN ('EXCHANGE','MONEY_REFUND')",
            name="ck_return_ref_requested_type",
        ),
    )
