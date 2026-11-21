"""`mod-chat` wiring: conversations, SSE turns, tool registry and the LLM loop.

Chat orchestrates every other module through their `public.py` and owns the
turn/action scopes (one DB session per streamed turn or direct UI action).
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.mod_auth.public import provide_phone_service
from src.modules.mod_cart.public import provide_cart_service
from src.modules.mod_catalog.public import build_tool_registry, provide_catalog_service
from src.modules.mod_chat import MODULE_ID
from src.modules.mod_chat.adapters.conversation_context_adapter import (
    ConversationContextAdapter,
)
from src.modules.mod_chat.adapters.feedback_postgres_adapter import FeedbackPostgresAdapter
from src.modules.mod_chat.adapters.http.attachment_router import create_attachment_router
from src.modules.mod_chat.adapters.http.chatbot_router import (
    ActionScope,
    TurnScope,
    create_chatbot_router,
)
from src.modules.mod_chat.adapters.http.feedback_router import create_feedback_router
from src.modules.mod_chat.adapters.openai_nlp_adapter import OpenAILLMProvider
from src.modules.mod_chat.adapters.pillow_image_processor import PillowImageProcessor
from src.modules.mod_chat.adapters.reaction_postgres_adapter import ReactionPostgresAdapter
from src.modules.mod_chat.adapters.registry_tool_adapter import RegistryToolAdapter
from src.modules.mod_chat.application.attachment_service import AttachmentService
from src.modules.mod_chat.application.dispatch_direct_action_use_case import (
    DispatchDirectActionUseCase,
)
from src.modules.mod_chat.application.feedback_service import FeedbackService
from src.modules.mod_chat.application.interpret_and_respond_use_case import (
    InterpretAndRespondUseCase,
)
from src.modules.mod_chat.application.manage_conversation_use_case import (
    ManageConversationUseCase,
)
from src.modules.mod_chat.application.tools.action_dispatcher import ActionDispatcher
from src.modules.mod_chat.application.tools.cart_handlers import build_cart_handlers
from src.modules.mod_chat.application.tools.catalog_handlers import build_catalog_handlers
from src.modules.mod_chat.application.tools.order_handlers import build_order_handlers
from src.modules.mod_chat.application.tools.phone_handlers import build_phone_handlers
from src.modules.mod_chat.ports.llm_provider_port import LLMProvider
from src.modules.mod_chat.ports.tool_executor_port import ToolExecutorPort
from src.modules.mod_orders.public import provide_orders_service
from src.shared.application.tools.tool_registry import ToolRegistry
from src.shared.domain.sensitive_data_filter import SensitiveDataFilter
from src.shared.infrastructure.config.settings import Settings, get_settings
from src.shared.infrastructure.db import connection
from src.shared.infrastructure.db.connection import get_session
from src.shared.infrastructure.di.capabilities import (
    ACTION_SCOPE,
    ATTACHMENT_CLEANUP,
    CONVERSATION_CONTEXT,
    FEEDBACK_NOTIFIER,
)
from src.shared.infrastructure.di.registry import registry
from src.shared.infrastructure.http.dependencies.rate_limiter import provide_rate_limiter
from src.shared.infrastructure.http.router_options import RouterOptions
from src.shared.infrastructure.persistence.attachment_postgres_adapter import AttachmentPostgresAdapter
from src.shared.infrastructure.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)
from src.shared.infrastructure.persistence.message_postgres_adapter import MessagePostgresAdapter
from src.shared.infrastructure.persistence.outbox_postgres_adapter import OutboxPostgresAdapter
from src.shared.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.shared.infrastructure.storage.local_attachment_storage import LocalAttachmentStorage


def provide_attachment_service(
    session: AsyncSession = Depends(get_session),
) -> AttachmentService:
    """Attachments (SPEC-23): local private storage and Pillow normalization."""
    settings = get_settings()
    return AttachmentService(
        attachments=AttachmentPostgresAdapter(session),
        conversations=ConversationPostgresAdapter(session),
        storage=LocalAttachmentStorage(
            settings.attachment_storage_path,
            public_base_url=settings.attachment_public_base_url,
            secret=settings.attachment_signing_secret,
        ),
        processor=PillowImageProcessor(
            max_bytes=settings.attachment_max_bytes,
            max_side=settings.attachment_max_side,
            stored_side=settings.attachment_stored_side,
            thumbnail_side=settings.attachment_thumbnail_side,
        ),
        uow=SqlAlchemyUnitOfWork(session),
        signed_url_ttl_seconds=settings.attachment_signed_url_ttl_seconds,
        max_images_per_turn=settings.attachment_max_images_per_turn,
    )


def provide_chatbot_service(
    session: AsyncSession = Depends(get_session),
) -> ManageConversationUseCase:
    return ManageConversationUseCase(
        conversations=ConversationPostgresAdapter(session),
        messages=MessagePostgresAdapter(session),
        uow=SqlAlchemyUnitOfWork(session),
        attachments=provide_attachment_service(session),
    )


def provide_feedback_service(
    session: AsyncSession = Depends(get_session),
) -> FeedbackService:
    """Reactions and suggestions/bugs (SPEC-24)."""
    settings = get_settings()
    notifier = (
        registry.resolve(FEEDBACK_NOTIFIER) if registry.is_registered(FEEDBACK_NOTIFIER) else None
    )
    return FeedbackService(
        reactions=ReactionPostgresAdapter(session),
        messages=MessagePostgresAdapter(session),
        conversations=ConversationPostgresAdapter(session),
        suggestions=FeedbackPostgresAdapter(session),
        outbox=OutboxPostgresAdapter(session),
        uow=SqlAlchemyUnitOfWork(session),
        storage=LocalAttachmentStorage(
            settings.attachment_storage_path,
            public_base_url=settings.attachment_public_base_url,
            secret=settings.attachment_signing_secret,
        ),
        processor=PillowImageProcessor(
            max_bytes=settings.attachment_max_bytes,
            max_side=settings.attachment_max_side,
            stored_side=settings.attachment_stored_side,
            thumbnail_side=settings.attachment_thumbnail_side,
        ),
        notifier=notifier,
        send_inline=settings.notification_send_inline,
    )


def provide_conversation_context(
    session: AsyncSession,
) -> ConversationContextAdapter:
    """Narrow chat-owned port consumed by cart/catalog through the registry."""
    return ConversationContextAdapter(
        ConversationPostgresAdapter(session),
        MessagePostgresAdapter(session),
    )


def provide_llm_provider(settings: Settings | None = None) -> LLMProvider:
    """Picks the LLM adapter by LLM_PROVIDER. A missing API key only fails when the provider is used."""
    settings = settings or get_settings()
    if settings.llm_provider == "openai":
        return OpenAILLMProvider(
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            timeout_seconds=settings.llm_timeout_seconds,
            vision_enabled=settings.llm_vision_enabled,
            reasoning_effort=settings.llm_reasoning_effort,
        )
    if settings.llm_provider == "claude":
        raise NotImplementedError("LLM_PROVIDER=claude is not implemented yet")
    raise ValueError(f"Unknown LLM_PROVIDER {settings.llm_provider!r} (supported: openai)")


def provide_tool_executor(session: AsyncSession) -> ToolExecutorPort:
    """Tool registry with handlers bound to this request's database session."""
    return RegistryToolAdapter(provide_tool_registry(session))


def provide_tool_registry(session: AsyncSession) -> ToolRegistry:
    """Build the canonical registry for both LLM and direct-UI action paths."""
    catalog = provide_catalog_service(session)
    cart = provide_cart_service(session)
    handlers: dict[str, Any] = {}
    handlers.update(build_catalog_handlers(catalog))
    handlers.update(build_cart_handlers(cart))
    handlers.update(build_order_handlers(provide_orders_service(session)))
    handlers.update(build_phone_handlers(provide_phone_service(session)))
    return build_tool_registry(handlers)


def provide_action_dispatcher(session: AsyncSession) -> ActionDispatcher:
    return ActionDispatcher(provide_tool_registry(session))


def sanitize_sensitive_text(text: str) -> str:
    """Redact customer secrets before they are persisted or sent to the LLM."""
    return SensitiveDataFilter().redactar(text).texto


def build_interpret_and_respond(session: AsyncSession) -> InterpretAndRespondUseCase:
    settings = get_settings()
    return InterpretAndRespondUseCase(
        conversations=ConversationPostgresAdapter(session),
        messages=MessagePostgresAdapter(session),
        uow=SqlAlchemyUnitOfWork(session),
        llm=provide_llm_provider(settings),
        tools=provide_tool_executor(session),
        assistant_name=settings.assistant_name,
        llm_timeout_seconds=settings.llm_timeout_seconds,
        sanitize_text=sanitize_sensitive_text,
        attachments=provide_attachment_service(session),
    )


def provide_interpret_and_respond(
    session: AsyncSession = Depends(get_session),
) -> InterpretAndRespondUseCase:
    return build_interpret_and_respond(session)


@asynccontextmanager
async def provide_turn_scope() -> AsyncIterator[InterpretAndRespondUseCase]:
    """One database session per use: a WebSocket outlives any request-scoped session, so each turn opens its own."""
    connection.get_engine()
    async with connection.open_session() as session:
        yield build_interpret_and_respond(session)


@asynccontextmanager
async def provide_action_scope() -> AsyncIterator[DispatchDirectActionUseCase]:
    """One database session for a direct UI action; no LLM or SSE is opened."""
    connection.get_engine()
    async with connection.open_session() as session:
        yield DispatchDirectActionUseCase(
            conversations=ConversationPostgresAdapter(session),
            messages=MessagePostgresAdapter(session),
            uow=SqlAlchemyUnitOfWork(session),
            dispatcher=provide_action_dispatcher(session),
            sanitize_text=sanitize_sensitive_text,
        )


def build_router(options: RouterOptions) -> APIRouter:
    settings = get_settings()
    router = APIRouter()
    router.include_router(
        create_chatbot_router(
            provide_chatbot_service,
            cookie_secure=settings.cookie_secure,
            turn_scope=options.turn_scope or provide_turn_scope,
            action_scope=options.action_scope,
            rate_limiter=provide_rate_limiter(),
        )
    )
    router.include_router(
        create_attachment_router(
            provide_attachment_service,
            _provide_storage,
            rate_limiter=provide_rate_limiter(),
        )
    )
    router.include_router(
        create_feedback_router(provide_feedback_service, rate_limiter=provide_rate_limiter())
    )
    return router


def _provide_storage() -> LocalAttachmentStorage:
    settings = get_settings()
    return LocalAttachmentStorage(
        settings.attachment_storage_path,
        public_base_url=settings.attachment_public_base_url,
        secret=settings.attachment_signing_secret,
    )


router = build_router(RouterOptions())


def configure(container) -> None:
    """Chat exposes the turn/action scopes, the context cart/catalog need and attachment cleanup."""
    container.register(ACTION_SCOPE, provide_action_scope)
    container.register(CONVERSATION_CONTEXT, provide_conversation_context)
    container.register(ATTACHMENT_CLEANUP, provide_attachment_service)


__all__ = [
    "ACTION_SCOPE",
    "ActionScope",
    "MODULE_ID",
    "TurnScope",
    "build_interpret_and_respond",
    "build_router",
    "configure",
    "provide_action_dispatcher",
    "provide_action_scope",
    "provide_attachment_service",
    "provide_chatbot_service",
    "provide_conversation_context",
    "provide_feedback_service",
    "provide_interpret_and_respond",
    "provide_llm_provider",
    "provide_tool_executor",
    "provide_tool_registry",
    "provide_turn_scope",
    "router",
    "sanitize_sensitive_text",
]
