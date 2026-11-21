"""Customer claims endpoints (SPEC-19, SPEC-20) proxied to G5 M2."""
from typing import Any, Callable, Protocol

from fastapi import APIRouter, Depends, Query, Request, status

from src.modules.mod_postventa import MODULE_ID
from src.modules.mod_postventa.adapters.http.postventa_errors import translate_postventa_error
from src.modules.mod_postventa.application.postventa_dto import (
    ClaimDetailResponse,
    ClaimListResponse,
    CreateClaimRequest,
)
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.infrastructure.http.dependencies.auth_guard import require_customer
from src.shared.infrastructure.http.dependencies.jwt_validator import get_bearer_token


class ClaimsServicePort(Protocol):
    async def create_claim(
        self,
        *,
        customer_id: Any,
        holder_token: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        ...

    async def list_claims(
        self,
        *,
        holder_token: str,
        estado: str | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> dict[str, Any]:
        ...

    async def get_claim(self, *, code: str, holder_token: str) -> dict[str, Any]:
        ...


def create_claims_router(service_provider: Callable[..., ClaimsServicePort]) -> APIRouter:
    router = APIRouter(prefix="/reclamos", tags=[MODULE_ID])

    @router.post("", status_code=status.HTTP_201_CREATED, response_model=ClaimDetailResponse)
    async def create_claim(
        payload: CreateClaimRequest,
        request: Request,
        caller: CallerIdentity = Depends(require_customer),
        service: ClaimsServicePort = Depends(service_provider),
    ) -> ClaimDetailResponse:
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not idempotency_key:
            from src.shared.infrastructure.http.problem import ProblemError

            raise ProblemError(
                status=400,
                code="VALIDACION",
                title="Invalid request",
                detail="Idempotency-Key is required.",
            )
        try:
            raw = await service.create_claim(
                customer_id=caller.customer,
                holder_token=get_bearer_token(request),
                payload=payload.model_dump(exclude_none=True),
                idempotency_key=idempotency_key,
            )
            return ClaimDetailResponse.model_validate(raw)
        except Exception as exc:
            raise translate_postventa_error(exc) from exc

    @router.get("", response_model=ClaimListResponse)
    async def list_claims(
        request: Request,
        estado: str | None = Query(default=None),
        page: int = Query(default=1, ge=1),
        pageSize: int = Query(default=10, ge=1, le=50),
        caller: CallerIdentity = Depends(require_customer),
        service: ClaimsServicePort = Depends(service_provider),
    ) -> ClaimListResponse:
        try:
            raw = await service.list_claims(
                holder_token=get_bearer_token(request),
                estado=estado,
                page=page,
                page_size=pageSize,
            )
            return ClaimListResponse.model_validate(raw)
        except Exception as exc:
            raise translate_postventa_error(exc) from exc

    @router.get("/{codigo}", response_model=ClaimDetailResponse)
    async def get_claim(
        codigo: str,
        request: Request,
        caller: CallerIdentity = Depends(require_customer),
        service: ClaimsServicePort = Depends(service_provider),
    ) -> ClaimDetailResponse:
        try:
            raw = await service.get_claim(
                code=codigo.strip(),
                holder_token=get_bearer_token(request),
            )
            return ClaimDetailResponse.model_validate(raw)
        except Exception as exc:
            raise translate_postventa_error(exc) from exc

    return router
