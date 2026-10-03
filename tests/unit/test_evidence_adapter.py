"""H2-06; referencias locales de SPEC-21, sin upload a Ventas."""

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.evidence_postgres_adapter import EvidencePostgresAdapter
from src.adapters.outbound.persistence.models import Evidence


@pytest.fixture
def session():
    return MagicMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_guardar_referencia_conserva_contrato_de_ventas(session):
    evidence = Evidence(
        conversation_id=uuid.uuid4(), type="IMAGE",
        url="https://ventas.example/evidence.jpg", original_file_name="foto.jpg",
        size_bytes=1024,
    )
    assert await EvidencePostgresAdapter(session).guardar(evidence) is evidence
    session.add.assert_called_once_with(evidence)
    session.flush.assert_awaited_once_with()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    assert evidence.url == "https://ventas.example/evidence.jpg"
    assert evidence.return_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tipo", ["IMAGEN", "IMAGE"])
async def test_tipo_de_ventas_se_persiste_en_ingles_y_se_traduce_al_enviar(session, tipo):
    adapter = EvidencePostgresAdapter(session)
    evidence = Evidence(type=tipo, conversation_id=uuid.uuid4())
    assert await adapter.guardar(evidence) is evidence
    assert evidence.type == "IMAGE"
    assert session.add.call_args.args[0].type == "IMAGE"
    assert adapter.tipo_hacia_ventas(evidence.type) == "IMAGEN"
    session.commit.assert_not_awaited()


def test_traduccion_no_inventa_literales_de_tipos_aun_no_confirmados():
    assert EvidencePostgresAdapter.tipo_desde_ventas("FUTURE_TYPE") == "FUTURE_TYPE"
    assert EvidencePostgresAdapter.tipo_hacia_ventas("FUTURE_TYPE") == "FUTURE_TYPE"


@pytest.mark.asyncio
async def test_listar_borrador_filtra_pendientes_de_esa_conversacion(session):
    """SPEC-21 · Req. 4 · Scenario: Ventas no disponible (referencias conservadas)."""
    conversation_id = uuid.uuid4()
    evidencias = [Evidence(conversation_id=conversation_id)]
    result = MagicMock()
    result.scalars.return_value.all.return_value = evidencias
    session.execute.return_value = result
    assert await EvidencePostgresAdapter(session).listar_borrador(conversation_id) == evidencias
    compiled = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert compiled.params["conversation_id_1"] == conversation_id
    assert "evidence.return_id IS NULL" in str(compiled)
    assert "ORDER BY evidence.created_at, evidence.id" in str(compiled)


@pytest.mark.asyncio
@pytest.mark.parametrize("encontrada", [True, False])
async def test_asociacion_atomica_filtra_id_conversacion_y_devolucion(session, encontrada):
    """SPEC-21 · Req. 4 · Scenario: Solicitud registrada (solo asociación local)."""
    evidence_id, conversation_id = uuid.uuid4(), uuid.uuid4()
    row = Evidence(id=evidence_id, return_id="DEV-1") if encontrada else None
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    session.execute.return_value = result
    adapter = EvidencePostgresAdapter(session)
    assert await adapter.asociar_devolucion(evidence_id, conversation_id, "DEV-1") is row
    stmt = session.execute.call_args.args[0]
    compiled = stmt.compile(dialect=postgresql.dialect())
    assert compiled.params["id_1"] == evidence_id
    assert compiled.params["conversation_id_1"] == conversation_id
    assert compiled.params["return_id_1"] == "DEV-1"
    assert compiled.params["return_id"] == "DEV-1"
    assert "evidence.return_id IS NULL OR evidence.return_id =" in str(compiled)
    assert "RETURNING evidence.id" in str(compiled)
    assert stmt.get_execution_options()["populate_existing"] is True
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
