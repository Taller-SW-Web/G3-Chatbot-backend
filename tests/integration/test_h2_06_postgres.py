"""Verificación real H2-06, solo con TEST_DATABASE_URL PostgreSQL 18+.

Padres mínimos y trigger en esquema exclusivo de test; no prueba H2-10.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from src.adapters.outbound.persistence.evidencia_postgres_adapter import EvidenciaPostgresAdapter
from src.adapters.outbound.persistence.mensaje_postgres_adapter import MensajePostgresAdapter
from src.adapters.outbound.persistence.models import Evidencia, Mensaje

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def crear_conversacion(session, metadata):
    conversacion_id = uuid.uuid4()
    await session.execute(insert(metadata.tables["conversacion"]).values(id=conversacion_id))
    return conversacion_id


def nueva_evidencia(conversacion_id, **cambios):
    datos = dict(
        conversacion_id=conversacion_id, tipo="IMAGEN",
        url="https://ventas.example/evidencia.jpg", nombre_archivo_original="foto.jpg",
        tamanio_bytes=1024,
    )
    datos.update(cambios)
    return Evidencia(**datos)


async def test_mensajes_historial_busqueda_defaults_y_aislamiento(postgres_session, persistence_metadata):
    """SPEC-05 Req. 1: persistencia al retomar; Req. 9: texto ya filtrado."""
    session = postgres_session
    a = await crear_conversacion(session, persistence_metadata)
    b = await crear_conversacion(session, persistence_metadata)
    adapter = MensajePostgresAdapter(session)
    inicio = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for minuto in range(4):
        await adapter.guardar(Mensaje(
            conversacion_id=a, rol="CLIENTE", texto="zapatillas [tarjeta oculta]",
            creado_en=inicio + timedelta(minutes=minuto), bloques=None, argumentos=None,
        ))
    await adapter.guardar(Mensaje(conversacion_id=b, rol="CLIENTE", texto="otra conversación"))
    mensajes = await adapter.listar_recientes(a, limite=2)
    assert [m.creado_en for m in mensajes] == [inicio + timedelta(minutes=i) for i in (2, 3)]
    assert all(m.conversacion_id == a and m.id.version == 7 for m in mensajes)
    assert all(m.bloques is None and m.argumentos is None for m in mensajes)
    encontrados = (await session.execute(
        select(Mensaje).where(
            Mensaje.conversacion_id == a,
            Mensaje.busqueda.op("@@")(text("plainto_tsquery('spanish', 'zapatilla')")),
        )
    )).scalars().all()
    assert len(encontrados) == 4
    # Verifica SQL NULL, no JSON null, en los valores opcionales.
    assert await session.scalar(text("SELECT count(*) FROM mensaje WHERE bloques IS NULL")) == 5


@pytest.mark.parametrize("cambios", [
    {"rol": "DESCONOCIDO"},
    {"rol": "HERRAMIENTA", "herramienta": None},
    {"tokens_entrada": -1}, {"tokens_salida": -1}, {"latencia_ms": -1},
    {"bloques": []}, {"argumentos": []},
])
async def test_mensaje_rechaza_datos_fuera_del_contrato(postgres_session, persistence_metadata, cambios):
    session = postgres_session
    conversacion_id = await crear_conversacion(session, persistence_metadata)
    datos = dict(conversacion_id=conversacion_id, rol="CLIENTE")
    datos.update(cambios)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await MensajePostgresAdapter(session).guardar(Mensaje(**datos))


@pytest.mark.parametrize("tamanio", [0, 5242881])
async def test_evidencia_rechaza_tamanio_invalido(postgres_session, persistence_metadata, tamanio):
    """SPEC-21 · Req. 3 · límite de 5 MB (constraint de persistencia)."""
    session = postgres_session
    conversacion_id = await crear_conversacion(session, persistence_metadata)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await EvidenciaPostgresAdapter(session).guardar(
                nueva_evidencia(conversacion_id, tamanio_bytes=tamanio)
            )


async def test_asociacion_evidencia_idempotencia_aislamiento_y_trigger(postgres_session, persistence_metadata):
    """SPEC-21 · Req. 4 · Scenario: Solicitud registrada (asociación local)."""
    session = postgres_session
    a = await crear_conversacion(session, persistence_metadata)
    b = await crear_conversacion(session, persistence_metadata)
    await session.execute(insert(persistence_metadata.tables["devolucion_ref"]), [
        {"devolucion_id": "DEV-1"}, {"devolucion_id": "DEV-2"},
    ])
    adapter = EvidenciaPostgresAdapter(session)
    evidencia = await adapter.guardar(nueva_evidencia(
        a, actualizado_en=datetime(2000, 1, 1, tzinfo=timezone.utc), tamanio_bytes=5242880,
    ))
    assert evidencia.id.version == 7
    assert await adapter.asociar_devolucion(evidencia.id, b, "DEV-1") is None
    assert len(await adapter.listar_borrador(a)) == 1
    asociada = await adapter.asociar_devolucion(evidencia.id, a, "DEV-1")
    assert asociada.devolucion_id == "DEV-1"
    assert asociada.actualizado_en.year > 2000
    assert asociada.tamanio_bytes == 5242880
    assert await adapter.listar_borrador(a) == []
    assert (await adapter.asociar_devolucion(evidencia.id, a, "DEV-1")).devolucion_id == "DEV-1"
    assert await adapter.asociar_devolucion(evidencia.id, a, "DEV-2") is None
    assert await adapter.asociar_devolucion(uuid.uuid4(), a, "DEV-1") is None


async def test_fk_mensaje_evidencia_y_restrict_devolucion(postgres_session, persistence_metadata):
    session = postgres_session
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await MensajePostgresAdapter(session).guardar(
                Mensaje(conversacion_id=uuid.uuid4(), rol="CLIENTE")
            )
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await EvidenciaPostgresAdapter(session).guardar(nueva_evidencia(uuid.uuid4()))
    conversacion_id = await crear_conversacion(session, persistence_metadata)
    adapter = EvidenciaPostgresAdapter(session)
    evidencia = await adapter.guardar(nueva_evidencia(conversacion_id, tamanio_bytes=1))
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await adapter.asociar_devolucion(evidencia.id, conversacion_id, "INEXISTENTE")
    await session.execute(insert(persistence_metadata.tables["devolucion_ref"]).values(devolucion_id="DEV-1"))
    await adapter.asociar_devolucion(evidencia.id, conversacion_id, "DEV-1")
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(text("DELETE FROM devolucion_ref WHERE devolucion_id = 'DEV-1'"))


async def test_borrado_conversacion_cascada(postgres_session, persistence_metadata):
    session = postgres_session
    conversacion_id = await crear_conversacion(session, persistence_metadata)
    await MensajePostgresAdapter(session).guardar(Mensaje(conversacion_id=conversacion_id, rol="CLIENTE"))
    await EvidenciaPostgresAdapter(session).guardar(nueva_evidencia(conversacion_id))
    await session.execute(persistence_metadata.tables["conversacion"].delete().where(
        persistence_metadata.tables["conversacion"].c.id == conversacion_id
    ))
    assert await session.scalar(text("SELECT count(*) FROM mensaje")) == 0
    assert await session.scalar(text("SELECT count(*) FROM evidencia")) == 0
