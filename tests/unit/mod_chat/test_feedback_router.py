"""Feedback routers: reactions (silent limit) and suggestions (multipart)."""
import uuid

import httpx
import pytest
from fastapi import FastAPI

from src.modules.mod_chat.adapters.http.feedback_router import create_feedback_router
from src.modules.mod_chat.application.feedback_service import FeedbackError
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.infrastructure.http.dependencies.caller_identity import get_caller_identity
from src.shared.infrastructure.http.problem import register_problem_handlers

CUSTOMER = uuid.uuid4()
MESSAGE = uuid.uuid4()


class StubService:
    def __init__(self):
        self.error = None
        self.reactions = []

    async def react(self, caller, message_id, reaction):
        if self.error:
            raise self.error
        self.reactions.append((message_id, reaction))
        return {"mensajeId": str(message_id), "reaccion": reaction}

    async def create_suggestion(self, customer_id, **kwargs):
        if self.error:
            raise self.error
        return {"sugerenciaId": str(uuid.uuid4()), "codigoSeguimiento": "SUG-ABC12345"}


def app_with(service, limiter=None):
    app = FastAPI()
    register_problem_handlers(app)
    app.dependency_overrides[get_caller_identity] = (
        lambda: CallerIdentity(customer_id=CUSTOMER)
    )
    app.include_router(
        create_feedback_router(lambda: service, rate_limiter=limiter), prefix="/api/v1"
    )
    return app


@pytest.mark.asyncio
async def test_reaction_returns_the_stored_value():
    service = StubService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with(service)), base_url="http://test"
    ) as client:
        res = await client.post(f"/api/v1/mensajes/{MESSAGE}/reaccion", json={"reaccion": "LIKE"})
    assert res.status_code == 200
    assert res.json()["reaccion"] == "POSITIVE"
    assert service.reactions == [(MESSAGE, "POSITIVE")]


@pytest.mark.asyncio
async def test_thirty_first_reaction_is_ignored_silently():
    from src.shared.infrastructure.http.dependencies.rate_limiter import (
        REACTIONS_PER_MINUTE,
        InMemoryRateLimiter,
    )

    limiter = InMemoryRateLimiter()
    for _ in range(REACTIONS_PER_MINUTE.limit):
        await limiter.hit(REACTIONS_PER_MINUTE, f"cust:{CUSTOMER}")
    service = StubService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with(service, limiter)), base_url="http://test"
    ) as client:
        res = await client.post(f"/api/v1/mensajes/{MESSAGE}/reaccion", json={"reaccion": "DISLIKE"})
    assert res.status_code == 204
    assert service.reactions == []


@pytest.mark.asyncio
async def test_suggestion_accepts_multipart_without_screenshot():
    service = StubService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with(service)), base_url="http://test"
    ) as client:
        res = await client.post(
            "/api/v1/sugerencias",
            headers={"Idempotency-Key": "sug-key"},
            data={
                "tipo": "Bug",
                "titulo": "No carga el carrito",
                "descripcion": "Pasa al abrir el carrito.",
                "correoContacto": "ana@example.com",
            },
        )
    assert res.status_code == 201
    assert res.json()["codigoSeguimiento"] == "SUG-ABC12345"


@pytest.mark.asyncio
async def test_suggestion_requires_idempotency_key():
    service = StubService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with(service)), base_url="http://test"
    ) as client:
        res = await client.post(
            "/api/v1/sugerencias",
            data={
                "tipo": "Bug", "titulo": "Título", "descripcion": "Suficiente texto.",
                "correoContacto": "ana@example.com",
            },
        )
    assert res.status_code == 400
    assert res.json()["code"] == "VALIDACION"


@pytest.mark.asyncio
async def test_sixth_suggestion_in_the_hour_is_429():
    from src.shared.infrastructure.http.dependencies.rate_limiter import (
        SUGGESTIONS_PER_HOUR,
        InMemoryRateLimiter,
    )

    limiter = InMemoryRateLimiter()
    for _ in range(SUGGESTIONS_PER_HOUR.limit):
        await limiter.hit(SUGGESTIONS_PER_HOUR, f"cust:{CUSTOMER}")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with(StubService(), limiter)), base_url="http://test"
    ) as client:
        res = await client.post(
            "/api/v1/sugerencias",
            headers={"Idempotency-Key": "too-many"},
            data={
                "tipo": "Bug", "titulo": "Título", "descripcion": "Suficiente texto.",
                "correoContacto": "ana@example.com",
            },
        )
    assert res.status_code == 429
    assert res.json()["code"] == "DEMASIADAS_SOLICITUDES"


@pytest.mark.asyncio
async def test_service_error_is_problem_json():
    service = StubService()
    service.error = FeedbackError("ADJUNTO_INVALIDO", "Captura inválida.", status=400)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with(service)), base_url="http://test"
    ) as client:
        res = await client.post(
            "/api/v1/sugerencias",
            headers={"Idempotency-Key": "bad-shot"},
            data={
                "tipo": "Bug", "titulo": "Título", "descripcion": "Suficiente texto.",
                "correoContacto": "ana@example.com",
            },
        )
    assert res.status_code == 400
    assert res.json()["code"] == "ADJUNTO_INVALIDO"
