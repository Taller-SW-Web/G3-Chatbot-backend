"""Tests H2-03 (Sonny): modelos cart + cart_item y adapters sin BD (offline).

Criterio hito-2.md: cada adapter con al menos un test.
El test de integracion contra Supabase (INSERT real + CHECKs + indices)
se hace en H2-10. Sigue el patron de test_conversation_adapter.py (H2-07).
"""
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.cart_postgres_adapter import CartPostgresAdapter
from src.adapters.outbound.persistence.cart_item_postgres_adapter import CartItemPostgresAdapter
from src.adapters.outbound.persistence.models import Base, Cart, CartItem, Conversation

cart_table = Base.metadata.tables["cart"]
item_table = Base.metadata.tables["cart_item"]


def _checks(table):
    return [str(c.sqltext) for c in table.constraints if isinstance(c, CheckConstraint)]


def _index(table, name):
    return next(i for i in table.indexes if i.name == name)


def test_modelos_registrados_en_base():
    assert Cart.__tablename__ == "cart"
    assert CartItem.__tablename__ == "cart_item"
    assert "cart" in Base.metadata.tables
    assert "cart_item" in Base.metadata.tables


def test_cart_columnas_y_nulabilidad():
    assert set(cart_table.columns.keys()) == {
        "id", "customer_id", "conversation_id", "status", "coupon_code",
        "address_id", "shipping_snapshot", "totals_snapshot", "version",
        "created_at", "updated_at",
    }
    for nullable in ("customer_id", "conversation_id", "coupon_code", "address_id",
                     "shipping_snapshot", "totals_snapshot"):
        assert cart_table.columns[nullable].nullable is True
    for required in ("status", "version", "created_at", "updated_at"):
        assert cart_table.columns[required].nullable is False
    assert isinstance(cart_table.columns["id"].type, UUID)
    assert isinstance(cart_table.columns["shipping_snapshot"].type, JSONB)
    assert isinstance(cart_table.columns["totals_snapshot"].type, JSONB)
    assert cart_table.columns["created_at"].type.timezone is True
    assert cart_table.columns["updated_at"].type.timezone is True


def test_cart_pk_y_defaults():
    assert list(cart_table.primary_key.columns.keys()) == ["id"]
    assert "uuidv7()" in str(cart_table.columns["id"].server_default.arg)
    assert "ACTIVE" in str(cart_table.columns["status"].server_default.arg)
    assert str(cart_table.columns["version"].server_default.arg) == "0"
    assert "now()" in str(cart_table.columns["created_at"].server_default.arg)
    assert "now()" in str(cart_table.columns["updated_at"].server_default.arg)
    # H2-10 instala el trigger; el modelo solo declara el valor generado.
    assert cart_table.columns["updated_at"].server_onupdate is not None


def test_cart_fk_resuelve_conversation():
    fk = next(iter(cart_table.c.conversation_id.foreign_keys))
    assert fk.column is Conversation.__table__.c.id
    assert fk.ondelete == "CASCADE"


def test_cart_sin_fk_externas():
    # customer_id (sub) y address_id (Seguridad) son referencias sin FK.
    assert {fk.parent.name for fk in cart_table.foreign_keys} == {"conversation_id"}


def test_cart_check_constraints():
    checks = _checks(cart_table)
    assert any("ACTIVE" in s and "MERGED" in s for s in checks)
    assert any("jsonb_typeof(shipping_snapshot)" in s for s in checks)
    assert any("jsonb_typeof(totals_snapshot)" in s for s in checks)
    assert any("customer_id IS NOT NULL OR conversation_id IS NOT NULL" in s for s in checks)


def test_cart_indices_parciales():
    cliente = _index(cart_table, "uq_cart_customer_open")
    assert cliente.unique is True
    assert list(cliente.columns.keys()) == ["customer_id"]
    assert "ACTIVE" in str(cliente.dialect_options["postgresql"]["where"])
    anonimo = _index(cart_table, "uq_cart_conversation_open")
    assert anonimo.unique is True
    assert list(anonimo.columns.keys()) == ["conversation_id"]
    assert "customer_id IS NULL" in str(anonimo.dialect_options["postgresql"]["where"])
    assert "ix_cart_conversation_id" in {i.name for i in cart_table.indexes}
    expiracion = _index(cart_table, "ix_cart_anonymous_updated_at")
    assert "customer_id IS NULL" in str(expiracion.dialect_options["postgresql"]["where"])


def test_cart_item_columnas_y_nulabilidad():
    assert set(item_table.columns.keys()) == {
        "id", "cart_id", "product_id", "sku", "name", "variant_description",
        "image_url", "quantity", "unit_price_ref", "created_at", "updated_at",
    }
    for nullable in ("variant_description", "image_url"):
        assert item_table.columns[nullable].nullable is True
    for required in ("cart_id", "product_id", "sku", "name", "quantity",
                     "unit_price_ref", "created_at", "updated_at"):
        assert item_table.columns[required].nullable is False


def test_cart_item_pk_fk_y_checks():
    assert list(item_table.primary_key.columns.keys()) == ["id"]
    assert "uuidv7()" in str(item_table.columns["id"].server_default.arg)
    fk = next(iter(item_table.c.cart_id.foreign_keys))
    assert fk.column is Cart.__table__.c.id
    assert fk.ondelete == "CASCADE"
    uniques = [c for c in item_table.constraints if isinstance(c, UniqueConstraint)]
    assert {u.name for u in uniques} == {"uq_cart_item_cart_sku"}
    checks = _checks(item_table)
    assert any("quantity BETWEEN 1 AND 10" in s for s in checks)
    assert any("unit_price_ref >= 0" in s for s in checks)


def test_adapters_exponen_operaciones():
    for method in ("guardar", "obtener_por_id", "obtener_abierto_por_cliente",
                   "obtener_abierto_por_conversacion", "cambiar_estado"):
        assert hasattr(CartPostgresAdapter, method)
    for method in ("guardar", "obtener_por_id", "obtener_por_cart_y_sku",
                   "listar_por_cart", "eliminar"):
        assert hasattr(CartItemPostgresAdapter, method)


@pytest.fixture
def session():
    return MagicMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_cart_guardar_hace_flush_sin_commit(session):
    """SPEC-11: la transaccion la controla el caso de uso."""
    row = Cart(customer_id=uuid.uuid4())
    assert await CartPostgresAdapter(session).guardar(row) is row
    session.add.assert_called_once_with(row)
    session.flush.assert_awaited_once_with()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cart_item_agregar_mismo_sku_suma_en_caso_de_uso(session):
    """SPEC-11 Req.1 Scenario: SKU ya existente (el adapter expone la busqueda)."""
    cart_id = uuid.uuid4()
    existente = CartItem(cart_id=cart_id, sku="SKU-A")
    result = MagicMock()
    result.scalar_one_or_none.return_value = existente
    session.execute.return_value = result
    assert await CartItemPostgresAdapter(session).obtener_por_cart_y_sku(cart_id, "SKU-A") is existente
    compiled = str(session.execute.call_args.args[0])
    assert "cart_item.cart_id" in compiled and "cart_item.sku" in compiled
    session.commit.assert_not_awaited()
