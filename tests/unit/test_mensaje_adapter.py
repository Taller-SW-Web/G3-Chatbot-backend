"""H2-06; persistencia parcial de SPEC-05 (no prueba el motor completo)."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.mensaje_postgres_adapter import MensajePostgresAdapter
from src.adapters.outbound.persistence.models import Mensaje


@pytest.fixture
def session():
    return MagicMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_guardar_texto_ya_redactado_sin_cerrar_transaccion(session):
    """SPEC-05 · Req. 9 · Scenario: El cliente escribe un número de tarjeta en el chat.

    Verifica solo que persistencia conserva el texto filtrado por el llamador.
    """
    mensaje = Mensaje(conversacion_id=uuid.uuid4(), rol="CLIENTE", texto="[tarjeta oculta]")
    adapter = MensajePostgresAdapter(session)
    assert await adapter.guardar(mensaje) is mensaje
    session.add.assert_called_once_with(mensaje)
    session.flush.assert_awaited_once_with()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    assert mensaje.texto == "[tarjeta oculta]"


@pytest.mark.asyncio
async def test_historial_reciente_filtra_conversacion_y_devuelve_orden_cronologico(session):
    """SPEC-05 · Req. 1 · Scenario: Cambiar a una conversación anterior (parte persistencia)."""
    conversacion_id = uuid.uuid4()
    antiguos = [Mensaje(rol="CLIENTE"), Mensaje(rol="ASISTENTE")]
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(reversed(antiguos))
    session.execute.return_value = result
    assert await MensajePostgresAdapter(session).listar_recientes(conversacion_id) == antiguos
    stmt = session.execute.call_args.args[0]
    compiled = stmt.compile(dialect=postgresql.dialect())
    assert compiled.params["conversacion_id_1"] == conversacion_id
    assert compiled.params["param_1"] == 50
    assert "WHERE mensaje.conversacion_id =" in str(compiled)
    assert "ORDER BY mensaje.creado_en DESC, mensaje.id DESC" in str(compiled)
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_contexto_llm_puede_pedir_12_y_historial_vacio(session):
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result
    assert await MensajePostgresAdapter(session).listar_recientes(uuid.uuid4(), limite=12) == []
    compiled = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert compiled.params["param_1"] == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("limite", [0, -1, 51])
async def test_limite_invalido_no_consulta_bd(session, limite):
    with pytest.raises(ValueError, match="entre 1 y 50"):
        await MensajePostgresAdapter(session).listar_recientes(uuid.uuid4(), limite=limite)
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_error_de_flush_se_propaga_al_llamador(session):
    session.flush = AsyncMock(side_effect=RuntimeError("fallo de persistencia"))
    with pytest.raises(RuntimeError, match="fallo de persistencia"):
        await MensajePostgresAdapter(session).guardar(Mensaje(rol="CLIENTE"))
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
