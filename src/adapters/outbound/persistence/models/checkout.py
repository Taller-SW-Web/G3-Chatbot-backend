"""Modelo checkout (H2-04 David, SPEC-12, SPEC-14).

Fuente: G3-Chatbot-specs/docs/modelo-datos.md (seccion `checkout`)
+ openspec/specs/checkout-pago + direccion-cotizacion-envio.
- Un cart origina N checkouts (1:N): si uno expira/falla se reintenta
  con el mismo carrito. Un solo checkout vivo por carrito (UQ parcial).
- customer_id es el `sub` del token, sin FK hacia Seguridad (ADR-0013).
- summary es el snapshot enviado a Ventas (lineas, descuentos, cupon,
  envio, direccion, contacto con tipoDocumento/numeroDocumento A14).
- payment_attempts es desnormalizacion deliberada de count(payment_attempt)
  para topear 3 intentos incluso concurrente.
- Sin order_id: la relacion vive en order_ref.checkout_id (UNIQUE).
- H2-10 debe instalar set_updated_at(); server_onupdate declara ese valor
  generado, no crea el trigger.
- PG18: id uuid DEFAULT uuidv7().
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, FetchedValue, ForeignKey, Index, Integer, Numeric, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.connection import Base


class Checkout(Base):
    __tablename__ = "checkout"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    cart_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cart.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # sub del token, sin FK (ADR-0013).
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'PENDING_PAYMENT'")
    )
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    payment_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
        server_onupdate=FetchedValue(),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING_PAYMENT','PAYMENT_APPROVED','CONFIRMED','FAILED','EXPIRED')",
            name="ck_checkout_status",
        ),
        CheckConstraint("jsonb_typeof(summary) = 'object'", name="ck_checkout_summary_object"),
        CheckConstraint("total >= 0", name="ck_checkout_total"),
        CheckConstraint(
            "payment_attempts BETWEEN 0 AND 3", name="ck_checkout_payment_attempts"
        ),
        # Un solo checkout vivo por carrito (anti doble-clic / reintentos).
        Index(
            "uq_checkout_cart_alive",
            "cart_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING_PAYMENT','PAYMENT_APPROVED')"),
        ),
        # Cubre la FK e historial de checkouts de un carrito.
        Index("ix_checkout_cart_id", "cart_id"),
        # Job de expiracion cada minuto.
        Index(
            "ix_checkout_expires_at",
            "expires_at",
            postgresql_where=text("status = 'PENDING_PAYMENT'"),
        ),
        # Idempotencia: misma clave devuelve el mismo checkout.
        Index("uq_checkout_idempotency_key", "idempotency_key", unique=True),
    )
