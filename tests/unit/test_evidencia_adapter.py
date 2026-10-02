"""H2-06; referencias locales de SPEC-21, sin upload a Ventas."""

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.evidencia_postgres_adapter import EvidenciaPostgresAdapter
from src.adapters.outbound.persistence.models import Evidencia


@pytest.fixture
def session():
    return MagicMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_guardar_referencia_conserva_contrato_de_ventas(session):
    evidencia = Evidencia(
        conversacion_id=uuid.uuid4(), tipo="IMAGEN",
        url="https://ventas.example/evidencia.jpg", nombre_archivo_original="foto.jpg",
        tamanio_bytes=1024,
    )
    assert await EvidenciaPostgresAdapter(session).guardar(evidencia) is evidencia
    session.add.assert_called_once_with(evidencia)
    session.flush.assert_awaited_once_with()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    assert evidencia.url == "https://ventas.example/evidencia.jpg"
    assert evidencia.devolucion_id is None


@pytest.mark.asyncio
async def test_listar_borrador_filtra_pendientes_de_esa_conversacion(session):
    """SPEC-21 · Req. 4 · Scenario: Ventas no disponible (referencias conservadas)."""
    conversacion_id = uuid.uuid4()
    evidencias = [Evidencia(conversacion_id=conversacion_id)]
    result = MagicMock()
    result.scalars.return_value.all.return_value = evidencias
    session.execute.return_value = result
    assert await EvidenciaPostgresAdapter(session).listar_borrador(conversacion_id) == evidencias
    compiled = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert compiled.params["conversacion_id_1"] == conversacion_id
    assert "evidencia.devolucion_id IS NULL" in str(compiled)
    assert "ORDER BY evidencia.creado_en, evidencia.id" in str(compiled)


@pytest.mark.asyncio
@pytest.mark.parametrize("encontrada", [True, False])
async def test_asociacion_atomica_filtra_id_conversacion_y_devolucion(session, encontrada):
    """SPEC-21 · Req. 4 · Scenario: Solicitud registrada (solo asociación local)."""
    evidencia_id, conversacion_id = uuid.uuid4(), uuid.uuid4()
    row = Evidencia(id=evidencia_id, devolucion_id="DEV-1") if encontrada else None
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    session.execute.return_value = result
    adapter = EvidenciaPostgresAdapter(session)
    assert await adapter.asociar_devolucion(evidencia_id, conversacion_id, "DEV-1") is row
    stmt = session.execute.call_args.args[0]
    compiled = stmt.compile(dialect=postgresql.dialect())
    assert compiled.params["id_1"] == evidencia_id
    assert compiled.params["conversacion_id_1"] == conversacion_id
    assert compiled.params["devolucion_id_1"] == "DEV-1"
    assert compiled.params["devolucion_id"] == "DEV-1"
    assert "evidencia.devolucion_id IS NULL OR evidencia.devolucion_id =" in str(compiled)
    assert "RETURNING evidencia.id" in str(compiled)
    assert stmt.get_execution_options()["populate_existing"] is True
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
