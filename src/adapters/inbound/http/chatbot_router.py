"""REST conversation endpoints (SPEC-05 Req. 1, contratos-integracion.md 2.1). Thin: no business rules."""
import secrets
import uuid
from collections.abc import Callable

from fastapi import APIRouter, Depends, Query, Response

from src.adapters.inbound.http.dependencies.caller_identity import (
    CHAT_SID_COOKIE,
    get_caller_identity,
    hash_chat_sid,
)
from src.adapters.inbound.http.problem import not_found
from src.adapters.inbound.http.schemas.conversation_schemas import (
    ConversationCreatedOut,
    ConversationSummaryOut,
    MessageOut,
)
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.inbound.chatbot_service_port import ChatbotServicePort, ConversationNotFoundError

# Anonymous conversations expire after 7 days of inactivity (modelo-datos.md); provisional cookie lifetime.
CHAT_SID_MAX_AGE_SECONDS = 7 * 24 * 3600


def create_chatbot_router(
    service_provider: Callable[..., ChatbotServicePort], *, cookie_secure: bool
) -> APIRouter:
    """`service_provider` is a FastAPI dependency; infrastructure wires it, tests replace it."""
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

    return router
