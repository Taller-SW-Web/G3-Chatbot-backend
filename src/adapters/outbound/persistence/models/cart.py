"""Modelo cart (H2-03, SPEC-11).

Fuente: G3-Chatbot-specs/docs/modelo-datos.md (seccion `cart`).
- Cabecera del carrito: anonimo (por conversacion, expira 7d) o cliente
  (unico ACTIVE/IN_CHECKOUT persistente entre sesiones).
- customer_id es el `sub` del token (uuid), sin FK hacia Seguridad (ADR-0013).
- address_id es referencia a Seguridad, sin FK.
- shipping_snapshot / totals_snapshot son snapshots jsonb con los nombres
  reales del contrato (idZona, costoEnvio, ...), no se renombran.
- version es bloqueo optimista para mutaciones concurrentes.
- H2-10 debe instalar set_updated_at(); server_onupdate declara ese valor
  generado, no crea el trigger.
- PG18: id uuid DEFAULT uuidv7().
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, FetchedValue, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.connection import Base


class Cart(Base):
    __tablename__ = "cart"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    # sub del token, sin FK (ADR-0013). Null si el carrito es anonimo.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Solo en carritos anonimos: al expirar la conversacion, se va su carrito.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversation.id", ondelete="CASCADE"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ACTIVE'"))
    # Cupon aplicado (validado contra Productos, no consumido aun).
    coupon_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Referencia a una direccion en Seguridad, sin FK.
    address_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Snapshot de la cotizacion de Despacho con nombres reales del contrato.
    shipping_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Ultimo calculo: subtotal, descuentos, envio y total.
    totals_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Bloqueo optimista: dos mutaciones simultaneas no pierden cambios.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
        server_onupdate=FetchedValue(),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE','IN_CHECKOUT','CONVERTED','ABANDONED','MERGED')",
            name="ck_cart_status",
        ),
        CheckConstraint(
            "jsonb_typeof(shipping_snapshot) = 'object'", name="ck_cart_shipping_snapshot_object"
        ),
        CheckConstraint(
            "jsonb_typeof(totals_snapshot) = 'object'", name="ck_cart_totals_snapshot_object"
        ),
        CheckConstraint("version >= 0", name="ck_cart_version"),
        CheckConstraint(
            "customer_id IS NOT NULL OR conversation_id IS NOT NULL", name="ck_cart_owner"
        ),
        # Un solo carrito abierto por cliente. Incluye IN_CHECKOUT porque, si el
        # checkout falla o expira, el carrito vuelve a ACTIVE sin chocar.
        Index(
            "uq_cart_customer_open",
            "customer_id",
            unique=True,
            postgresql_where=text("status IN ('ACTIVE','IN_CHECKOUT')"),
        ),
        # Un solo carrito anonimo abierto por conversacion (fusion al login).
        Index(
            "uq_cart_conversation_open",
            "conversation_id",
            unique=True,
            postgresql_where=text("customer_id IS NULL AND status IN ('ACTIVE','IN_CHECKOUT')"),
        ),
        # Cubre la FK (borrado en cascada de conversaciones anonimas).
        Index("ix_cart_conversation_id", "conversation_id"),
        # Job de expiracion de carritos anonimos (7 dias).
        Index(
            "ix_cart_anonymous_updated_at",
            "updated_at",
            postgresql_where=text("customer_id IS NULL AND status = 'ACTIVE'"),
        ),
    )
