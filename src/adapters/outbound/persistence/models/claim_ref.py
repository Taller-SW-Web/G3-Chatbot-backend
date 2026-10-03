"""Modelo claim_ref (H2-04 David, SPEC-19, SPEC-20).

Fuente: G3-Chatbot-specs/docs/modelo-datos.md (seccion `claim_ref`).
- Referencia local al reclamo creado en Ventas. La verdad del reclamo
  vive en Ventas; aqui idempotencia + pertenencia al cliente.
- claim_id es el ID de Ventas (text) y es la PK. code es el codigo
  visible para el cliente (UNIQUE).
- order_id es el ID de Ventas SIN FK: el pedido puede venir de otro canal
  y no tener fila en order_ref.
- customer_id es el `sub` del token, sin FK hacia Seguridad (ADR-0013).
- Solo insercion: sin updated_at.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.connection import Base


class ClaimRef(Base):
    __tablename__ = "claim_ref"

    # PK con el ID de Ventas (text, formato del dueno).
    claim_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # Codigo visible para el cliente.
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # ID de Ventas, sin FK (puede ser de otro canal).
    order_id: Mapped[str] = mapped_column(Text, nullable=False)
    # sub del token, sin FK (ADR-0013).
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
