"""Modelo cart_item (H2-03, SPEC-10, SPEC-11).

Fuente: G3-Chatbot-specs/docs/modelo-datos.md (seccion `cart_item`).
- Linea del carrito. Agregar el mismo SKU suma cantidad, no duplica.
- product_id es referencia a Productos (text, formato del dueno), sin FK.
- name / variant_description / image_url son snapshots para mostrar;
  unit_price_ref es el ultimo precio visto y se recalcula en cada lectura.
- H2-10 debe instalar set_updated_at(); server_onupdate declara ese valor
  generado, no crea el trigger.
- PG18: id uuid DEFAULT uuidv7().
"""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, FetchedValue, ForeignKey, Index, Integer, Numeric, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.db.connection import Base


class CartItem(Base):
    __tablename__ = "cart_item"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    cart_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cart.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Referencia a Productos, sin FK (ADR-0013).
    product_id: Mapped[str] = mapped_column(Text, nullable=False)
    sku: Mapped[str] = mapped_column(Text, nullable=False)
    # Snapshot para mostrar (SPEC-11 Req.3).
    name: Mapped[str] = mapped_column(Text, nullable=False)
    variant_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # Ultimo precio visto; se recalcula contra Productos en cada lectura.
    unit_price_ref: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    # created_at reemplaza a added_at.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
        server_onupdate=FetchedValue(),
    )

    __table_args__ = (
        UniqueConstraint("cart_id", "sku", name="uq_cart_item_cart_sku"),
        CheckConstraint("quantity BETWEEN 1 AND 10", name="ck_cart_item_quantity"),
        CheckConstraint("unit_price_ref >= 0", name="ck_cart_item_unit_price_ref"),
        # Cubre la FK y el listado de lineas del carrito.
        Index("ix_cart_item_cart_id", "cart_id"),
    )
