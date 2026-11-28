"""REST conversation endpoints (SPEC-05 Req. 1, contratos-integracion.md 2.1). Thin: no business rules."""
import secrets
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, model_validator

from src.modules.mod_chat import MODULE_ID
from src.modules.mod_chat.adapters.http.schemas.conversation_schemas import (
    ConversationCreatedOut,
    ConversationSearchResultOut,
    ConversationSummaryOut,
    MessageOut,
)
from src.modules.mod_chat.adapters.http.sse import TurnStreamingResponse
from src.modules.mod_chat.application.attachment_service import AttachmentError
from src.modules.mod_chat.application.dispatch_direct_action_use_case import DispatchDirectActionUseCase
from src.modules.mod_chat.application.dto.incoming_message_dto import ToolAction
from src.modules.mod_chat.application.interpret_and_respond_use_case import (
    InterpretAndRespondUseCase,
    InvalidMessageError,
)
from src.modules.mod_chat.ports.chatbot_service_port import ChatbotServicePort, ConversationNotFoundError
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.infrastructure.http.dependencies.caller_identity import (
    CHAT_SID_COOKIE,
    get_caller_identity,
    hash_chat_sid,
)
from src.shared.infrastructure.http.dependencies.rate_limiter import (
    CHAT_MESSAGES_PER_MINUTE,
    InMemoryRateLimiter,
    client_ip,
)
from src.shared.infrastructure.http.openapi import problem_responses
from src.shared.infrastructure.http.problem import ProblemError, not_found

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
    adjuntoIds: list[str] | None = None

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
    rate_limiter: InMemoryRateLimiter | None = None,
) -> APIRouter:
    """`service_provider` is a FastAPI dependency; infrastructure wires it, tests replace it.
    `turn_scope` opens a session for SSE turns; `action_scope` opens one for direct UI actions."""
    router = APIRouter(prefix="/chat/conversaciones", tags=[MODULE_ID])
    limiter = rate_limiter or InMemoryRateLimiter()

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

    @router.get("/buscar", response_model=list[ConversationSearchResultOut])
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
        return [ConversationSearchResultOut.from_domain(c) for c in summaries]

    @router.get(
        "/{conversation_id}/mensajes",
        response_model=list[MessageOut],
        responses=problem_responses(404),
    )
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

        @router.post(
            "/{conversation_id}/mensajes",
            responses=problem_responses(404, 409, 422, 429, 501, 503),
        )
        async def send_message(
            conversation_id: uuid.UUID,
            body: SendMessageIn,
            request: Request,
            caller: CallerIdentity | None = Depends(get_caller_identity),
        ):
            """Phase 1 (validation, ownership, persisting the message) finishes before the response starts, so
            its errors are ordinary problem+json. The turn then streams as Server-Sent Events (see sse.py)."""
            if caller and caller.customer_id:
                limit_key = f"cust:{caller.customer_id}"
            elif caller and caller.anonymous_sid:
                limit_key = f"anon:{caller.anonymous_sid}"
            else:
                limit_key = f"ip:{client_ip(request)}"
            retry_after = await limiter.hit(CHAT_MESSAGES_PER_MINUTE, limit_key)
            if retry_after is not None:
                raise ProblemError(
                    429,
                    "DEMASIADAS_SOLICITUDES",
                    "Too many requests",
                    "Vas muy rápido, espera un momento antes de enviar otro mensaje.",
                    headers=[("Retry-After", str(retry_after))],
                    retryAfter=retry_after,
                )
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
                if len(body.adjuntoIds) > 3:
                    raise ProblemError(
                        422,
                        "LIMITE_ADJUNTOS",
                        "Unprocessable entity",
                        "Puedes adjuntar hasta 3 imágenes por mensaje.",
                    )
                if not body.adjuntoIds and not (body.texto or "").strip():
                    raise ProblemError(
                        400, "VALIDACION", "Invalid request", "Envía un texto o al menos una imagen."
                    )
            elif not (body.texto or "").strip():
                raise ProblemError(
                    400, "VALIDACION", "Invalid request", "El mensaje no puede estar vacío."
                )
            attachment_ids: list[uuid.UUID] = []
            for raw_id in body.adjuntoIds or []:
                try:
                    attachment_ids.append(uuid.UUID(str(raw_id)))
                except (ValueError, AttributeError, TypeError):
                    raise not_found("Uno de los adjuntos no existe.") from None
            resources = AsyncExitStack()
            try:
                use_case = await resources.enter_async_context(turn_scope())
                stream = await use_case.send_message(
                    caller, conversation_id, body.texto or "", attachment_ids=attachment_ids
                )
            except AttachmentError as exc:
                await resources.aclose()
                raise ProblemError(exc.status, exc.code, "Attachment error", exc.detail) from None
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
