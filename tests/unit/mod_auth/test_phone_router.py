"""Phone OTP router: status codes and problem+json (SPEC-04)."""
import uuid

import httpx
import pytest
from fastapi import FastAPI

from src.modules.mod_auth.adapters.http.phone_router import create_phone_router
from src.modules.mod_auth.application.manage_phone_verification_use_case import (
    PhoneVerificationError,
)
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.infrastructure.http.dependencies.caller_identity import get_caller_identity
from src.shared.infrastructure.http.problem import register_problem_handlers


class StubService:
    def __init__(self):
        self.answer = {
            "enviado": True,
            "celularEnmascarado": "+51 9****4321",
            "expiraEn": "2026-11-21T10:05:00+00:00",
            "codigo": "123456",
        }
        self.error = None

    async def request_otp(self, token, customer_id):
        if self.error:
            raise self.error
        return self.answer

    async def verify_otp(self, token, customer_id, code):
        if self.error:
            raise self.error
        return {"celularVerificado": True, "celularEnmascarado": "+51 9****4321"}


def app_with(service):
    app = FastAPI()
    register_problem_handlers(app)
    customer = uuid.uuid4()

    async def caller():
        return CallerIdentity(customer_id=customer, access_token="token")

    app.dependency_overrides[get_caller_identity] = caller
    app.include_router(create_phone_router(lambda: service), prefix="/api/v1")
    return app


@pytest.mark.asyncio
async def test_request_otp_returns_202_with_masked_phone():
    service = StubService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with(service)), base_url="http://test"
    ) as client:
        res = await client.post(
            "/api/v1/contacto/celular/solicitar-otp",
            headers={"Authorization": "Bearer token"},
        )
    assert res.status_code == 202
    assert res.json()["celularEnmascarado"] == "+51 9****4321"


@pytest.mark.asyncio
async def test_verify_otp_returns_the_verified_state():
    service = StubService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with(service)), base_url="http://test"
    ) as client:
        res = await client.post(
            "/api/v1/contacto/celular/verificar-otp",
            json={"codigo": "123456"},
            headers={"Authorization": "Bearer token"},
        )
    assert res.status_code == 200
    assert res.json()["celularVerificado"] is True


@pytest.mark.asyncio
async def test_wrong_code_is_401_with_remaining_attempts():
    service = StubService()
    service.error = PhoneVerificationError(
        "CODIGO_INVALIDO", "Código incorrecto.", status=401, extra={"intentosRestantes": 2}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with(service)), base_url="http://test"
    ) as client:
        res = await client.post(
            "/api/v1/contacto/celular/verificar-otp",
            json={"codigo": "000000"},
            headers={"Authorization": "Bearer token"},
        )
    assert res.status_code == 401
    assert res.headers["content-type"] == "application/problem+json"
    assert res.json()["code"] == "CODIGO_INVALIDO"
    assert res.json()["intentosRestantes"] == 2


@pytest.mark.asyncio
async def test_resend_limit_is_429_with_retry_after():
    service = StubService()
    service.error = PhoneVerificationError(
        "LIMITE_REENVIOS", "Espera.", status=429, extra={"reintentarEn": 60}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with(service)), base_url="http://test"
    ) as client:
        res = await client.post(
            "/api/v1/contacto/celular/solicitar-otp",
            headers={"Authorization": "Bearer token"},
        )
    assert res.status_code == 429
    assert res.headers["Retry-After"] == "60"
