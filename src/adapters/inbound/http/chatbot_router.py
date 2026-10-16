"""REST conversation endpoints (SPEC-05 Req. 1, contratos-integracion.md 2.1). Thin: no business rules."""
import secrets
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, model_validator

from src.adapters.inbound.http.dependencies.caller_identity import (
    CHAT_SID_COOKIE,
    get_caller_identity,
    hash_chat_sid,
)
from src.adapters.inbound.http.problem import ProblemError, not_found
from src.adapters.inbound.http.schemas.conversation_schemas import (
    ConversationCreatedOut,
    ConversationSummaryOut,
    MessageOut,
)
from src.adapters.inbound.http.sse import TurnStreamingResponse
from src.application.dto.incoming_message_dto import ToolAction
from src.application.use_cases.dispatch_direct_action_use_case import DispatchDirectActionUseCase
from src.application.use_cases.interpret_and_respond_use_case import (
    InterpretAndRespondUseCase,
    InvalidMessageError,
)
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.inbound.chatbot_service_port import ChatbotServicePort, ConversationNotFoundError

# Provisional: the specs leave the search query length undefined.
SEARCH_QUERY_MAX_CHARS = 100

# Anonymous conversations expire after 7 days of inactivity (modelo-datos.md); provisional cookie lifetime.
CHAT_SID_MAX_AGE_SECONDS = 7 * 24 * 3600

# Opens one database session and yields the use case bound to it; the session closes on exit.
TurnScope = Callable[[], AbstractAsyncContextManager[InterpretAndRespondUseCase]]
ActionScope = Callable[[], AbstractAsyncContextManager[DispatchDirectActionUseCase]]


class SendMessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    texto: str | None = None
    accion: ToolAction | None = None
    adjuntoIds: Any = None

    @model_validator(mode="after")
    def _reject_action_with_text(self) -> "SendMessageIn":
        if self.accion is not None and (self.texto is not None or self.adjuntoIds is not None):
            raise ValueError("body cannot combine accion with texto or adjuntoIds")
        return self


def create_chatbot_router(
    service_provider: Callable[..., ChatbotServicePort],
    *,
    cookie_secure: bool,
    turn_scope: TurnScope | None = None,
    action_scope: ActionScope | None = None,
) -> APIRouter:
    """`service_provider` is a FastAPI dependency; infrastructure wires it, tests replace it.
    `turn_scope` opens a session for SSE turns; `action_scope` opens one for direct UI actions."""
    router = APIRouter(prefix="/chat/conversaciones", tags=["conversaciones"])

    @router.post("", status_code=201, response_model=ConversationCreatedOut)
    async def create_conversation(
        response: Response,
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: ChatbotServicePort = Depends(service_provider),
    ):
        if caller is None:
            raw_sid = secrets.token_urlsafe(32)
            caller = CallerIdentity(anonymous_sid=hash_chat_sid(raw_sid))
            response.set_cookie(
                CHAT_SID_COOKIE, raw_sid, max_age=CHAT_SID_MAX_AGE_SECONDS,
                httponly=True, samesite="lax", secure=cookie_secure,
            )
        conversation = await service.create_conversation(caller)
        return ConversationCreatedOut(conversacionId=conversation.id)

    @router.get("", response_model=list[ConversationSummaryOut])
    async def list_conversations(
        pagina: int = Query(1, ge=1),
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: ChatbotServicePort = Depends(service_provider),
    ):
        summaries = await service.list_conversations(caller, pagina)
        return [ConversationSummaryOut.from_domain(c) for c in summaries]

    @router.get("/buscar", response_model=list[ConversationSummaryOut])
    async def search_conversations(
        q: str = Query(..., max_length=SEARCH_QUERY_MAX_CHARS),
        pagina: int = Query(1, ge=1),
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: ChatbotServicePort = Depends(service_provider),
    ):
        query = q.strip()
        if not query:
            raise ProblemError(400, "VALIDACION", "Invalid request", "`q` must not be blank.")
        summaries = await service.search_conversations(caller, query, pagina)
        return [ConversationSummaryOut.from_domain(c) for c in summaries]

    @router.get("/{conversation_id}/mensajes", response_model=list[MessageOut])
    async def get_history(
        conversation_id: uuid.UUID,
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: ChatbotServicePort = Depends(service_provider),
    ):
        try:
            messages = await service.get_history(caller, conversation_id)
        except ConversationNotFoundError:
            raise not_found() from None
        return [MessageOut.from_domain(m) for m in messages]

    if turn_scope is not None:

        @router.post("/{conversation_id}/mensajes")
        async def send_message(
            conversation_id: uuid.UUID,
            body: SendMessageIn,
            caller: CallerIdentity | None = Depends(get_caller_identity),
        ):
            """Phase 1 (validation, ownership, persisting the message) finishes before the response starts, so
            its errors are ordinary problem+json. The turn then streams as Server-Sent Events (see sse.py)."""
            if body.accion is not None:
                if action_scope is None:
                    raise ProblemError(
                        501, "FUNCIONALIDAD_NO_DISPONIBLE", "Not implemented",
                        "Direct actions are not available yet.",
                    )
                action_resources = AsyncExitStack()
                try:
                    action_use_case = await action_resources.enter_async_context(action_scope())
                    result = await action_use_case.execute(
                        caller,
                        conversation_id,
                        body.accion.model_dump(),
                    )
                except ConversationNotFoundError:
                    await action_resources.aclose()
                    raise not_found() from None
                except BaseException:
                    await action_resources.aclose()
                    raise
                await action_resources.aclose()
                return {
                    "code": result.code,
                    "mensajeId": str(result.message.id),
                    "texto": result.text,
                    "bloques": list(result.blocks),
                    "datos": result.data,
                }
            if body.adjuntoIds is not None:
                raise ProblemError(
                    501, "FUNCIONALIDAD_NO_DISPONIBLE", "Not implemented",
                    "Attachments are not available yet; send only `texto`.",
                )
            resources = AsyncExitStack()
            try:
                use_case = await resources.enter_async_context(turn_scope())
                stream = await use_case.send_message(caller, conversation_id, body.texto or "")
            except InvalidMessageError as exc:
                await resources.aclose()
                raise ProblemError(400, "VALIDACION", "Invalid request", str(exc)) from None
            except ConversationNotFoundError:
                await resources.aclose()
                raise not_found() from None
            except BaseException:
                await resources.aclose()
                raise
            return TurnStreamingResponse(stream, resources)

    return router
