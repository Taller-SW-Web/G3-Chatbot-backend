"""Attachment endpoints (SPEC-23): upload, delete, signed URL and private file read."""
from __future__ import annotations

import mimetypes
import uuid
from collections.abc import Callable

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status

from src.modules.mod_chat import MODULE_ID
from src.modules.mod_chat.application.attachment_service import AttachmentError, AttachmentService
from src.shared.application.ports.attachment_storage_port import AttachmentStoragePort
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.infrastructure.http.dependencies.caller_identity import get_caller_identity
from src.shared.infrastructure.http.dependencies.rate_limiter import (
    ATTACHMENT_UPLOADS_PER_MINUTE,
    InMemoryRateLimiter,
    client_ip,
)
from src.shared.infrastructure.http.problem import ProblemError


def _problem(exc: AttachmentError) -> ProblemError:
    title = {
        400: "Invalid attachment",
        404: "Resource not found",
        409: "Conflict",
        422: "Unprocessable entity",
        503: "Service unavailable",
    }.get(exc.status, "Attachment error")
    return ProblemError(exc.status, exc.code, title, exc.detail)


def _rate_key(caller: CallerIdentity | None, request: Request) -> str:
    if caller and caller.customer_id:
        return f"cust:{caller.customer_id}"
    if caller and caller.anonymous_sid:
        return f"anon:{caller.anonymous_sid}"
    return f"ip:{client_ip(request)}"


def create_attachment_router(
    service_provider: Callable[..., AttachmentService],
    storage_provider: Callable[..., AttachmentStoragePort],
    *,
    rate_limiter: InMemoryRateLimiter | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/chat", tags=[MODULE_ID])
    limiter = rate_limiter or InMemoryRateLimiter()

    @router.post(
        "/conversaciones/{conversation_id}/adjuntos",
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_attachment(
        conversation_id: uuid.UUID,
        request: Request,
        archivo: UploadFile = File(...),
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: AttachmentService = Depends(service_provider),
    ) -> dict:
        retry_after = await limiter.hit(
            ATTACHMENT_UPLOADS_PER_MINUTE, _rate_key(caller, request)
        )
        if retry_after is not None:
            raise ProblemError(
                429,
                "DEMASIADAS_SOLICITUDES",
                "Too many requests",
                "Vas muy rápido, espera un momento.",
                headers=[("Retry-After", str(retry_after))],
                retryAfter=retry_after,
            )
        content = await archivo.read()
        try:
            return await service.upload(caller, conversation_id, content)
        except AttachmentError as exc:
            raise _problem(exc) from exc

    @router.delete(
        "/conversaciones/{conversation_id}/adjuntos/{attachment_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_attachment(
        conversation_id: uuid.UUID,
        attachment_id: uuid.UUID,
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: AttachmentService = Depends(service_provider),
    ) -> Response:
        try:
            await service.delete(caller, conversation_id, attachment_id)
        except AttachmentError as exc:
            raise _problem(exc) from exc
        return Response(status_code=204)

    # Static route before `/{attachment_id}` so "archivo" never matches an id.
    @router.get("/adjuntos/archivo")
    async def read_signed_file(
        clave: str = Query(..., min_length=1, max_length=300),
        exp: int = Query(..., ge=0),
        sig: str = Query(..., min_length=64, max_length=64),
        storage: AttachmentStoragePort = Depends(storage_provider),
    ) -> Response:
        if not storage.verificar_firma(clave, exp, sig):
            raise ProblemError(403, "FIRMA_INVALIDA", "Forbidden", "La URL firmada no es válida.")
        try:
            data = await storage.leer(clave)
        except Exception:
            raise ProblemError(404, "RECURSO_NO_ENCONTRADO", "Resource not found", None) from None
        media_type = mimetypes.guess_type(clave)[0] or "application/octet-stream"
        return Response(content=data, media_type=media_type, headers={"Cache-Control": "private, max-age=60"})

    @router.get("/adjuntos/{attachment_id}")
    async def signed_urls(
        attachment_id: uuid.UUID,
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: AttachmentService = Depends(service_provider),
    ) -> dict:
        try:
            return await service.signed_urls(caller, attachment_id)
        except AttachmentError as exc:
            raise _problem(exc) from exc

    return router
