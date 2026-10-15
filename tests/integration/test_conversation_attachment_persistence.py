"""Verificación real H2-06, solo con TEST_DATABASE_URL PostgreSQL 18+.

Conversation/Attachment reales y return_ref mínimo en esquema exclusivo; no prueba H2-10.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from src.adapters.outbound.persistence.evidence_postgres_adapter import EvidencePostgresAdapter
from src.adapters.outbound.persistence.message_postgres_adapter import MessagePostgresAdapter
from src.adapters.outbound.persistence.models import Evidence, Message

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def crear_conversacion(session, metadata):
    conversation_id = uuid.uuid4()
    await session.execute(insert(metadata.tables["conversation"]).values(
        id=conversation_id, anonymous_sid=f"test_{conversation_id.hex}",
    ))
    return conversation_id


def nueva_evidencia(conversation_id, **cambios):
    datos = dict(
        conversation_id=conversation_id, type="IMAGE",
        url="https://ventas.example/evidence.jpg", original_file_name="foto.jpg",
        size_bytes=1024,
    )
    datos.update(cambios)
    return Evidence(**datos)


async def test_mensajes_historial_busqueda_defaults_y_aislamiento(postgres_session, persistence_metadata):
    """SPEC-05 Req. 1: persistencia al retomar; Req. 9: content ya filtrado."""
    session = postgres_session
    a = await crear_conversacion(session, persistence_metadata)
    b = await crear_conversacion(session, persistence_metadata)
    adapter = MessagePostgresAdapter(session)
    inicio = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for minuto in range(4):
        await adapter.guardar(Message(
            conversation_id=a, role="CUSTOMER", content="zapatillas [tarjeta oculta]",
            created_at=inicio + timedelta(minutes=minuto), blocks=None, arguments=None,
        ))
    await adapter.guardar(Message(conversation_id=b, role="CUSTOMER", content="otra conversación"))
    mensajes = await adapter.listar_recientes(a, limite=2)
    assert [m.created_at for m in mensajes] == [inicio + timedelta(minutes=i) for i in (2, 3)]
    assert all(m.conversation_id == a and m.id.version == 7 for m in mensajes)
    assert all(m.blocks is None and m.arguments is None for m in mensajes)
    encontrados = (await session.execute(
        select(Message).where(
            Message.conversation_id == a,
            Message.search_vector.op("@@")(text("plainto_tsquery('spanish', 'zapatilla')")),
        )
    )).scalars().all()
    assert len(encontrados) == 4
    # Verifica SQL NULL, no JSON null, en los valores opcionales.
    assert await session.scalar(text("SELECT count(*) FROM message WHERE blocks IS NULL")) == 5


@pytest.mark.parametrize("cambios", [
    {"role": "DESCONOCIDO"},
    {"role": "TOOL", "tool_name": None},
    {"input_tokens": -1}, {"output_tokens": -1}, {"latency_ms": -1},
    {"blocks": []}, {"arguments": []},
])
async def test_mensaje_rechaza_datos_fuera_del_contrato(postgres_session, persistence_metadata, cambios):
    session = postgres_session
    conversation_id = await crear_conversacion(session, persistence_metadata)
    datos = dict(conversation_id=conversation_id, role="CUSTOMER")
    datos.update(cambios)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await MessagePostgresAdapter(session).guardar(Message(**datos))


@pytest.mark.parametrize("tamanio", [0, 5242881])
async def test_evidencia_rechaza_tamanio_invalido(postgres_session, persistence_metadata, tamanio):
    """SPEC-21 · Req. 3 · límite de 5 MB (constraint de persistencia)."""
    session = postgres_session
    conversation_id = await crear_conversacion(session, persistence_metadata)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await EvidencePostgresAdapter(session).guardar(
                nueva_evidencia(conversation_id, size_bytes=tamanio)
            )


async def test_asociacion_evidencia_idempotencia_aislamiento_y_trigger(postgres_session, persistence_metadata):
    """SPEC-21 · Req. 4 · Scenario: Solicitud registrada (asociación local)."""
    session = postgres_session
    a = await crear_conversacion(session, persistence_metadata)
    b = await crear_conversacion(session, persistence_metadata)
    await session.execute(insert(persistence_metadata.tables["return_ref"]), [
        {"return_id": "DEV-1"}, {"return_id": "DEV-2"},
    ])
    adapter = EvidencePostgresAdapter(session)
    evidence = await adapter.guardar(nueva_evidencia(
        a, updated_at=datetime(2000, 1, 1, tzinfo=timezone.utc), size_bytes=5242880,
    ))
    assert evidence.id.version == 7
    assert await adapter.asociar_devolucion(evidence.id, b, "DEV-1") is None
    assert len(await adapter.listar_borrador(a)) == 1
    asociada = await adapter.asociar_devolucion(evidence.id, a, "DEV-1")
    assert asociada.return_id == "DEV-1"
    assert asociada.updated_at.year > 2000
    assert asociada.size_bytes == 5242880
    assert await adapter.listar_borrador(a) == []
    assert (await adapter.asociar_devolucion(evidence.id, a, "DEV-1")).return_id == "DEV-1"
    assert await adapter.asociar_devolucion(evidence.id, a, "DEV-2") is None
    assert await adapter.asociar_devolucion(uuid.uuid4(), a, "DEV-1") is None


async def test_fk_mensaje_evidencia_y_restrict_devolucion(postgres_session, persistence_metadata):
    session = postgres_session
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await MessagePostgresAdapter(session).guardar(
                Message(conversation_id=uuid.uuid4(), role="CUSTOMER")
            )
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await EvidencePostgresAdapter(session).guardar(nueva_evidencia(uuid.uuid4()))
    conversation_id = await crear_conversacion(session, persistence_metadata)
    adapter = EvidencePostgresAdapter(session)
    evidence = await adapter.guardar(nueva_evidencia(conversation_id, size_bytes=1))
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await adapter.asociar_devolucion(evidence.id, conversation_id, "INEXISTENTE")
    await session.execute(insert(persistence_metadata.tables["return_ref"]).values(return_id="DEV-1"))
    await adapter.asociar_devolucion(evidence.id, conversation_id, "DEV-1")
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(text("DELETE FROM return_ref WHERE return_id = 'DEV-1'"))


async def test_borrado_conversacion_cascada(postgres_session, persistence_metadata):
    session = postgres_session
    conversation_id = await crear_conversacion(session, persistence_metadata)
    await MessagePostgresAdapter(session).guardar(Message(conversation_id=conversation_id, role="CUSTOMER"))
    await EvidencePostgresAdapter(session).guardar(nueva_evidencia(conversation_id))
    await session.execute(persistence_metadata.tables["conversation"].delete().where(
        persistence_metadata.tables["conversation"].c.id == conversation_id
    ))
    assert await session.scalar(text("SELECT count(*) FROM message")) == 0
    assert await session.scalar(text("SELECT count(*) FROM evidence")) == 0
