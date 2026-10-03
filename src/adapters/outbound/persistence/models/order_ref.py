"""Modelo order_ref (H2-03, SPEC-15).

Fuente: G3-Chatbot-specs/docs/modelo-datos.md (seccion `order_ref`).
- Referencia local al pedido creado en Ventas. La verdad del pedido vive
  en Ventas; aqui solo idempotencia y pertenencia para seguimiento.
- order_id es el ID de Ventas (text, formato del dueno) y es la PK.
- checkout_id tiene UNIQUE: un checkout genera como maximo un pedido.
  FK como cadena: el modelo checkout lo implementa David (H2-04).
- local_status guarda el valor en ingles; el adapter traduce en ambos
  sentidos el literal de Ventas (ANULADO <-> CANCELLED).
- H2-10 debe instalar set_updated_at(); server_onupdate declara ese valor
  generado, no crea el trigger.
"""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, FetchedValue, ForeignKey, Index, Numeric, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.connection import Base


class OrderRef(Base):
    __tablename__ = "order_ref"

    # PK con el ID de Ventas (text, formato del dueno).
    order_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # sub del token, sin FK (ADR-0013).
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # FK como cadena: checkout se agrega en H2-04 (David).
    checkout_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("checkout.id", ondelete="RESTRICT"),
        nullable=False,
    )
    local_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'CREATED'")
    )
    total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
        server_onupdate=FetchedValue(),
    )

    __table_args__ = (
        UniqueConstraint("checkout_id", name="uq_order_ref_checkout_id"),
        CheckConstraint(
            "local_status IN ('CREATED','PAYMENT_PENDING_NOTIFICATION','PAID_NOTIFIED','CANCELLATION_REQUESTED','CANCELLED')",
            name="ck_order_ref_local_status",
        ),
        CheckConstraint("total >= 0", name="ck_order_ref_total"),
        # Encontrar el pedido de un checkout. Cubre la FK.
        Index("ix_order_ref_checkout_id", "checkout_id"),
        # Control de pertenencia al consultar seguimiento (junto con order_id).
        Index("ix_order_ref_customer_id", "customer_id"),
    )
