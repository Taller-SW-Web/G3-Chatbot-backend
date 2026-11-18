"""Engine y sesiones async: fabrica perezosa, cierre de sesion y serializacion (ADR-0011)."""
from decimal import Decimal

import pytest

from src.infrastructure.db import connection


class FakeSession:
    """Sesion minima: solo interesa que se cierre al salir del contexto."""

    closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        self.closed = True
        return False


@pytest.fixture(autouse=True)
def engine_aislado(monkeypatch):
    """Cada test arranca sin engine: el global vive en el proceso."""
    monkeypatch.setattr(connection, "engine", None)
    monkeypatch.setattr(connection, "AsyncSessionLocal", None)


def test_get_database_url_usa_la_variable_de_entorno(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@host:5432/db")
    assert connection.get_database_url() == "postgresql+asyncpg://u:p@host:5432/db"


def test_get_database_url_tiene_valor_por_defecto(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert connection.get_database_url().startswith("postgresql+asyncpg://")


def test_json_serializer_convierte_decimal_a_texto():
    assert connection._json_serializer({"precio": Decimal("19.90")}) == '{"precio": "19.90"}'


def test_create_engine_construye_sin_conectar():
    engine = connection.create_engine("postgresql+asyncpg://u:p@localhost:5432/db")
    assert engine is not None
    assert engine.url.drivername == "postgresql+asyncpg"


def test_get_engine_construye_una_sola_vez(monkeypatch):
    creados = []

    def fake_create_engine(url):
        creados.append(url)
        return object()

    monkeypatch.setattr(connection, "create_engine", fake_create_engine)
    monkeypatch.setattr(connection, "async_sessionmaker", lambda *a, **kw: "factory")

    primero = connection.get_engine()

    assert connection.get_engine() is primero
    assert len(creados) == 1


def test_get_session_factory_es_idempotente(monkeypatch):
    monkeypatch.setattr(connection, "create_engine", lambda url: object())
    monkeypatch.setattr(connection, "async_sessionmaker", lambda *a, **kw: "factory")

    assert connection.get_session_factory() == "factory"
    assert connection.get_session_factory() == "factory"


def test_open_session_cierra_al_salir(monkeypatch):
    sesion = FakeSession()
    monkeypatch.setattr(connection, "create_engine", lambda url: object())
    monkeypatch.setattr(connection, "async_sessionmaker", lambda *a, **kw: lambda: sesion)

    async def escenario():
        async with connection.open_session() as abierta:
            assert abierta is sesion
        assert sesion.closed

    import asyncio

    asyncio.run(escenario())


def test_get_session_entrega_y_cierra_la_sesion(monkeypatch):
    sesion = FakeSession()
    monkeypatch.setattr(connection, "create_engine", lambda url: object())
    monkeypatch.setattr(connection, "async_sessionmaker", lambda *a, **kw: lambda: sesion)

    async def escenario():
        generador = connection.get_session()
        assert await generador.__anext__() is sesion
        with pytest.raises(StopAsyncIteration):
            await generador.__anext__()
        assert sesion.closed

    import asyncio

    asyncio.run(escenario())
