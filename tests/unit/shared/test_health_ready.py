"""`/health/ready` runs SELECT 1 and answers 503 when the database fails; `/health` is unchanged."""
import httpx
import pytest
from fastapi import FastAPI

from src.shared.infrastructure.config.settings import Settings, get_settings
from src.shared.infrastructure.db.connection import get_session
from src.shared.infrastructure.http.health_router import router


class FakeSession:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.statements: list[str] = []

    async def execute(self, statement):
        self.statements.append(str(statement))
        if self.error:
            raise self.error


def _app(session: FakeSession) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_ready_is_200_after_select_1():
    session = FakeSession()
    res = await _get(_app(session), "/health/ready")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "database": "up"}
    assert session.statements == ["SELECT 1"]


@pytest.mark.asyncio
async def test_ready_is_503_when_the_database_fails_without_leaking_details():
    res = await _get(_app(FakeSession(ConnectionRefusedError("password=hunter2@db"))), "/health/ready")
    assert res.status_code == 503
    assert res.json() == {"status": "unavailable", "database": "down"}


@pytest.mark.asyncio
async def test_liveness_never_touches_the_database():
    session = FakeSession(ConnectionRefusedError())
    res = await _get(_app(session), "/health")
    assert res.status_code == 200
    assert session.statements == []


@pytest.mark.asyncio
async def test_liveness_declares_the_effective_email_mode(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("EMAIL_MODE", raising=False)
    try:
        res = await _get(_app(FakeSession()), "/health")
        assert res.status_code == 200
        assert res.json()["emailMode"] == "fake"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_liveness_declares_an_explicit_fake_mode_in_production(monkeypatch):
    import src.shared.infrastructure.http.health_router as health_router

    monkeypatch.setattr(
        health_router,
        "get_settings",
        lambda: Settings(
            app_env="production",
            email_mode="fake",
            attachment_storage="supabase",
            supabase_url="https://storage.example.com",
            supabase_service_key="service-key-value",
        ),
    )

    res = await _get(_app(FakeSession()), "/health")

    assert res.status_code == 200
    assert res.json()["emailMode"] == "fake"
