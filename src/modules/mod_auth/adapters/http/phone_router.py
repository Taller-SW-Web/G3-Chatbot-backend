"""Local phone OTP endpoints (SPEC-04): `/contacto/celular/*`.

Thin adapter: the OTP rules live in `ManagePhoneVerificationUseCase`. The code
never round-trips through the LLM or the `message` table.
"""
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from src.modules.mod_auth import MODULE_ID
from src.modules.mod_auth.application.manage_phone_verification_use_case import (
    ManagePhoneVerificationUseCase,
    PhoneVerificationError,
)
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.domain.module_errors import ModuleRequestError, ModuleUnavailableError
from src.shared.infrastructure.http.dependencies.auth_guard import require_customer
from src.shared.infrastructure.http.dependencies.jwt_validator import get_bearer_token
from src.shared.infrastructure.http.openapi import problem_responses
from src.shared.infrastructure.http.problem import ProblemError


class SolicitarOtpOut(BaseModel):
    enviado: bool = True
    celularEnmascarado: str
    expiraEn: str
    # Only with MODO_QA=true (SPEC-04 RNF, test mechanism).
    codigo: str | None = None


class VerificarOtpIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    codigo: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class VerificarOtpOut(BaseModel):
    celularVerificado: bool
    celularEnmascarado: str


def _translate(exc: Exception) -> ProblemError:
    if isinstance(exc, ProblemError):
        return exc
    if isinstance(exc, PhoneVerificationError):
        headers = None
        if exc.code == "LIMITE_REENVIOS" and exc.extra.get("reintentarEn"):
            headers = [("Retry-After", str(exc.extra["reintentarEn"]))]
        return ProblemError(
            exc.status,
            exc.code,
            "Phone verification failed",
            exc.detail,
            extra=exc.extra,
            headers=headers,
        )
    if isinstance(exc, ModuleRequestError):
        return ProblemError(exc.status, exc.code, "Module request failed")
    if isinstance(exc, ModuleUnavailableError):
        return ProblemError(
            503,
            "SERVICIO_NO_DISPONIBLE",
            "Service unavailable",
            "No pudimos confirmar tu celular ahora.",
            extra={"modulo": exc.module},
        )
    return ProblemError(
        503,
        "SERVICIO_NO_DISPONIBLE",
        "Service unavailable",
        "No pudimos completar la verificación en este momento.",
    )


def create_phone_router(
    phone_provider: Callable[..., ManagePhoneVerificationUseCase],
) -> APIRouter:
    router = APIRouter(
        prefix="/contacto/celular",
        tags=[MODULE_ID],
        responses=problem_responses(409, 410, 429, 503),
    )

    @router.post("/solicitar-otp", response_model=SolicitarOtpOut, status_code=status.HTTP_202_ACCEPTED)
    async def request_otp(
        request: Request,
        caller: CallerIdentity = Depends(require_customer),
        service: ManagePhoneVerificationUseCase = Depends(phone_provider),
    ) -> Any:
        try:
            return await service.request_otp(get_bearer_token(request), caller.customer)
        except Exception as exc:
            raise _translate(exc) from exc

    @router.post("/verificar-otp", response_model=VerificarOtpOut)
    async def verify_otp(
        body: VerificarOtpIn,
        request: Request,
        caller: CallerIdentity = Depends(require_customer),
        service: ManagePhoneVerificationUseCase = Depends(phone_provider),
    ) -> Any:
        try:
            return await service.verify_otp(
                get_bearer_token(request), caller.customer, body.codigo
            )
        except Exception as exc:
            raise _translate(exc) from exc

    return router
