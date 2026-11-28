"""Customer returns, evidence upload and return status endpoints (SPEC-21, SPEC-22)."""
from typing import Any, Callable, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status

from src.modules.mod_postventa import MODULE_ID
from src.modules.mod_postventa.adapters.http.postventa_errors import translate_postventa_error
from src.modules.mod_postventa.application.postventa_dto import (
    CreateReturnRequest,
    EvidenceResponse,
    ReturnDetailResponse,
    ReturnListResponse,
)
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.infrastructure.http.dependencies.auth_guard import require_customer
from src.shared.infrastructure.http.dependencies.jwt_validator import get_bearer_token
from src.shared.infrastructure.http.problem import ProblemError

MAX_EVIDENCE_BYTES = 5 * 1024 * 1024
ALLOWED_EVIDENCE_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}


class ReturnsServicePort(Protocol):
    async def upload_evidence(
        self,
        *,
        holder_token: str,
        file_name: str,
        content_type: str,
        content: bytes,
        conversation_id: UUID | None = None,
        customer_id: UUID | None = None,
    ) -> dict[str, Any]:
        ...

    async def create_return(
        self,
        *,
        customer_id: Any,
        holder_token: str,
        payload: dict[str, Any],
        idempotency_key: str,
        conversation_id: UUID | None = None,
    ) -> dict[str, Any]:
        ...

    async def list_returns(
        self,
        *,
        holder_token: str,
        estado: str | None = None,
        tipo: str | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> dict[str, Any]:
        ...

    async def get_return(self, *, return_id: str, holder_token: str) -> dict[str, Any]:
        ...


def create_returns_router(service_provider: Callable[..., ReturnsServicePort]) -> APIRouter:
    router = APIRouter(tags=[MODULE_ID])

    @router.post("/evidencias", status_code=status.HTTP_201_CREATED, response_model=EvidenceResponse)
    async def upload_evidence(
        request: Request,
        file: UploadFile = File(...),
        conversacionId: UUID | None = Form(default=None),
        caller: CallerIdentity = Depends(require_customer),
        service: ReturnsServicePort = Depends(service_provider),
    ) -> EvidenceResponse:
        content_type = (file.content_type or "").lower()
        if content_type not in ALLOWED_EVIDENCE_TYPES:
            raise ProblemError(
                status=400,
                code="EVIDENCIA_INVALIDA",
                title="Invalid evidence",
                detail="El archivo debe ser JPG, PNG, WEBP o PDF.",
            )
        content = await file.read()
        if not content or len(content) > MAX_EVIDENCE_BYTES:
            raise ProblemError(
                status=400,
                code="EVIDENCIA_INVALIDA",
                title="Invalid evidence",
                detail="El archivo debe pesar entre 1 byte y 5 MB.",
            )
        try:
            raw = await service.upload_evidence(
                holder_token=get_bearer_token(request),
                file_name=file.filename or "evidencia",
                content_type=content_type,
                content=content,
                conversation_id=conversacionId,
                customer_id=caller.customer,
            )
            return EvidenceResponse.model_validate(raw)
        except Exception as exc:
            raise translate_postventa_error(exc) from exc

    @router.post("/devoluciones", status_code=status.HTTP_201_CREATED, response_model=ReturnDetailResponse)
    async def create_return(
        payload: CreateReturnRequest,
        request: Request,
        caller: CallerIdentity = Depends(require_customer),
        service: ReturnsServicePort = Depends(service_provider),
    ) -> ReturnDetailResponse:
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not idempotency_key:
            raise ProblemError(
                status=400,
                code="VALIDACION",
                title="Invalid request",
                detail="Idempotency-Key is required.",
            )
        try:
            raw = await service.create_return(
                customer_id=caller.customer,
                holder_token=get_bearer_token(request),
                payload=payload.model_dump(exclude_none=True, exclude={"conversacionId"}),
                idempotency_key=idempotency_key,
                conversation_id=payload.conversacionId,
            )
            return ReturnDetailResponse.model_validate(raw)
        except Exception as exc:
            raise translate_postventa_error(exc) from exc

    @router.get("/devoluciones", response_model=ReturnListResponse)
    async def list_returns(
        request: Request,
        estado: str | None = Query(default=None),
        tipo: str | None = Query(default=None),
        page: int = Query(default=1, ge=1),
        pageSize: int = Query(default=10, ge=1, le=50),
        caller: CallerIdentity = Depends(require_customer),
        service: ReturnsServicePort = Depends(service_provider),
    ) -> ReturnListResponse:
        try:
            raw = await service.list_returns(
                holder_token=get_bearer_token(request),
                estado=estado,
                tipo=tipo,
                page=page,
                page_size=pageSize,
            )
            return ReturnListResponse.model_validate(raw)
        except Exception as exc:
            raise translate_postventa_error(exc) from exc

    @router.get("/devoluciones/{devolucion_id}", response_model=ReturnDetailResponse)
    async def get_return(
        devolucion_id: str,
        request: Request,
        caller: CallerIdentity = Depends(require_customer),
        service: ReturnsServicePort = Depends(service_provider),
    ) -> ReturnDetailResponse:
        try:
            raw = await service.get_return(
                return_id=devolucion_id.strip(),
                holder_token=get_bearer_token(request),
            )
            return ReturnDetailResponse.model_validate(raw)
        except Exception as exc:
            raise translate_postventa_error(exc) from exc

    return router
