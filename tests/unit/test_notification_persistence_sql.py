"""SQL of the SPEC-16 persistence methods, compiled for PostgreSQL (offline).

Trazabilidad: SPEC-16 Req. 2 (unicidad `(order_id, resend_number)` sin
abortar la transacción) y Req. 3 (deduplicación del reenvío por
Idempotency-Key en el payload del outbox `SEND_EMAIL`).
"""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from src.adapters.outbound.persistence.models import Notification
from src.adapters.outbound.persistence.notification_postgres_adapter import NotificationPostgresAdapter
from src.adapters.outbound.persistence.order_confirmation_postgres_adapter import (
    OrderConfirmationPostgresAdapter,
)
from src.adapters.outbound.persistence.outbox_postgres_adapter import OutboxPostgresAdapter


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _session(*results):
    session = MagicMock()
    session.execute = AsyncMock(side_effect=list(results))
    session.flush = AsyncMock()
    return session


def _result(scalar=None, first=None):
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    result.scalar_one.return_value = scalar
    result.first.return_value = first
    result.scalars.return_value.first.return_value = scalar
    return result


@pytest.mark.asyncio
async def test_crear_si_no_existe_usa_on_conflict_do_nothing():
    row = Notification(order_id="PED-1", type="ORDER_CONFIRMATION", resend_number=0,
                       recipient="maria@ejemplo.com", status="PENDING", attempts=0)
    session = _session(_result(), _result(scalar=row))

    record = await NotificationPostgresAdapter(session).crear_si_no_existe("PED-1", 0, "maria@ejemplo.com")

    insert_sql = _sql(session.execute.await_args_list[0].args[0])
    assert "ON CONFLICT ON CONSTRAINT uq_notification_order_resend DO NOTHING" in insert_sql
    assert "'ORDER_CONFIRMATION'" in insert_sql
    assert (record.status, record.attempts) == ("PENDING", 0)
    session.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_reenvio_se_registra_como_confirmation_resend():
    row = Notification(order_id="PED-1", type="CONFIRMATION_RESEND", resend_number=2,
                       recipient="maria@ejemplo.com", status="PENDING", attempts=0)
    session = _session(_result(), _result(scalar=row))

    await NotificationPostgresAdapter(session).crear_si_no_existe("PED-1", 2, "maria@ejemplo.com")

    assert "'CONFIRMATION_RESEND'" in _sql(session.execute.await_args_list[0].args[0])


@pytest.mark.asyncio
async def test_contar_reenvios_filtra_por_tipo():
    session = _session(_result(scalar=1))

    assert await NotificationPostgresAdapter(session).contar_reenvios("PED-1") == 1
    sql = _sql(session.execute.await_args.args[0])
    assert "notification.type = 'CONFIRMATION_RESEND'" in sql


@pytest.mark.asyncio
async def test_registrar_reintento_mantiene_pending_y_cerrar_fallida_no_suma():
    row = Notification(order_id="PED-1", type="ORDER_CONFIRMATION", resend_number=0,
                       recipient="maria@ejemplo.com", status="PENDING", attempts=2)
    session = _session(_result(scalar=row), _result(scalar=row))
    adapter = NotificationPostgresAdapter(session)

    await adapter.registrar_reintento("PED-1", 0, "smtp down")
    assert (row.status, row.attempts) == ("PENDING", 3)
    await adapter.cerrar_fallida("PED-1", 0, "smtp down")
    assert (row.status, row.attempts, row.last_error) == ("FAILED", 3, "smtp down")


@pytest.mark.asyncio
async def test_buscar_envio_por_clave_filtra_el_payload_jsonb():
    session = _session(_result(scalar=None))

    found = await OutboxPostgresAdapter(session).buscar_envio_por_clave("PED-1", str(uuid.UUID(int=1)), "k1")

    assert found is None
    sql = _sql(session.execute.await_args.args[0])
    assert "outbox.type = 'SEND_EMAIL'" in sql
    assert "(outbox.payload ->> 'idempotency_key') = 'k1'" in sql
    assert "(outbox.payload ->> 'order_id') = 'PED-1'" in sql


@pytest.mark.asyncio
async def test_cargar_snapshot_une_order_ref_y_checkout_con_el_pago_aprobado():
    session = _session(_result(first=None))

    assert await OrderConfirmationPostgresAdapter(session).cargar("PED-1") is None
    sql = _sql(session.execute.await_args.args[0])
    assert "JOIN checkout ON checkout.id = order_ref.checkout_id" in sql
    assert "order_ref.order_id = 'PED-1'" in sql
