"""Feedback endpoints: message reactions and suggestions/bugs (SPEC-24)."""
from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi import APIRouter, Depends, File, Form, Header, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from src.modules.mod_chat import MODULE_ID
from src.modules.mod_chat.application.feedback_service import FeedbackError, FeedbackService
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.infrastructure.http.dependencies.auth_guard import require_customer
from src.shared.infrastructure.http.dependencies.caller_identity import get_caller_identity
from src.shared.infrastructure.http.dependencies.rate_limiter import (
    REACTIONS_PER_MINUTE,
    SUGGESTIONS_PER_HOUR,
    InMemoryRateLimiter,
)
from src.shared.infrastructure.http.openapi import idempotency_key_required, problem_responses
from src.shared.infrastructure.http.problem import ProblemError


class ReactionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reaccion: str = Field(pattern=r"^(POSITIVE|NEGATIVE|LIKE|DISLIKE|like|dislike|positive|negative)$")


class ReactionOut(BaseModel):
    mensajeId: str
    reaccion: str


def _problem(exc: FeedbackError) -> ProblemError:
    title = {
        400: "Invalid request",
        404: "Resource not found",
        422: "Unprocessable entity",
        429: "Too many requests",
        503: "Service unavailable",
    }.get(exc.status, "Feedback error")
    return ProblemError(exc.status, exc.code, title, exc.detail)


def create_feedback_router(
    service_provider: Callable[..., FeedbackService],
    *,
    rate_limiter: InMemoryRateLimiter | None = None,
) -> APIRouter:
    router = APIRouter(tags=[MODULE_ID])
    limiter = rate_limiter or InMemoryRateLimiter()

    @router.post(
        "/mensajes/{message_id}/reaccion",
        response_model=ReactionOut,
        responses=problem_responses(404, 429),
    )
    async def react(
        message_id: uuid.UUID,
        body: ReactionIn,
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: FeedbackService = Depends(service_provider),
    ) -> Response | ReactionOut:
        key = (
            f"cust:{caller.customer_id}" if caller and caller.customer_id
            else f"anon:{caller.anonymous_sid}" if caller and caller.anonymous_sid
            else "anon:missing"
        )
        retry_after = await limiter.hit(REACTIONS_PER_MINUTE, key)
        if retry_after is not None:
            # Anti-abuse limit: ignored silently, never an error in the chat.
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        reaction = {"LIKE": "POSITIVE", "DISLIKE": "NEGATIVE"}.get(
            body.reaccion.upper(), body.reaccion.upper()
        )
        try:
            answer = await service.react(caller, message_id, reaction)
        except FeedbackError as exc:
            raise _problem(exc) from exc
        return ReactionOut(**answer)

    @router.post(
        "/sugerencias",
        status_code=status.HTTP_201_CREATED,
        responses=problem_responses(429, 503),
        openapi_extra=idempotency_key_required(),
    )
    async def create_suggestion(
        tipo: str = Form(...),
        titulo: str = Form(...),
        descripcion: str = Form(...),
        correoContacto: str = Form(...),
        captura: UploadFile | None = File(default=None),
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
        caller: CallerIdentity = Depends(require_customer),
        service: FeedbackService = Depends(service_provider),
    ) -> dict:
        if not idempotency_key:
            raise ProblemError(400, "VALIDACION", "Invalid request", "Idempotency-Key es obligatorio.")
        retry_after = await limiter.hit(SUGGESTIONS_PER_HOUR, f"cust:{caller.customer_id}")
        if retry_after is not None:
            raise ProblemError(
                429,
                "DEMASIADAS_SOLICITUDES",
                "Too many requests",
                "Ya enviaste varias sugerencias, intenta en un rato.",
                headers=[("Retry-After", str(retry_after))],
                retryAfter=retry_after,
            )
        content = await captura.read() if captura is not None else None
        try:
            return await service.create_suggestion(
                caller.customer,
                kind=tipo,
                title=titulo,
                description=descripcion,
                contact_email=correoContacto,
                idempotency_key=idempotency_key,
                screenshot=content,
            )
        except FeedbackError as exc:
            raise _problem(exc) from exc

    return router
