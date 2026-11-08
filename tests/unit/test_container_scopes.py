"""Sesion por uso: WebSocket y acciones directas abren y cierran la suya (ADR-0011)."""
import pytest

from src.infrastructure.config import container
from src.infrastructure.db import connection


class FakeSession:
    closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        self.closed = True
        return False


@pytest.mark.asyncio
async def test_turn_scope_abre_y_cierra_su_sesion(monkeypatch):
    sesion = FakeSession()
    construido = []
    monkeypatch.setattr(connection, "get_engine", lambda: None)
    monkeypatch.setattr(connection, "open_session", lambda: sesion)
    monkeypatch.setattr(container, "build_interpret_and_respond", lambda s: construido.append(s) or s)

    async with container.provide_turn_scope() as caso:
        assert caso is sesion
        assert sesion.closed is False

    assert construido == [sesion]
    assert sesion.closed


@pytest.mark.asyncio
async def test_action_scope_abre_y_cierra_su_sesion(monkeypatch):
    sesion = FakeSession()
    monkeypatch.setattr(connection, "get_engine", lambda: None)
    monkeypatch.setattr(connection, "open_session", lambda: sesion)
    monkeypatch.setattr(container, "provide_action_dispatcher", lambda s: "dispatcher")

    async with container.provide_action_scope() as caso:
        assert caso is not None
        assert sesion.closed is False

    assert sesion.closed
