"""Minimal dependency wiring (ADR-0002): builds use cases from a request-scoped DB session."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.fakes.fake_postventa import FakePostventaPort
from src.adapters.outbound.http_clients.postventa_client import PostventaClient
from src.adapters.outbound.nlp.openai_nlp_adapter import OpenAILLMProvider
from src.adapters.outbound.tools.registry_tool_adapter import RegistryToolAdapter
from src.application.tools.action_dispatcher import ActionDispatcher
from src.application.tools.cart_handlers import build_cart_handlers
from src.application.tools.catalog_handlers import build_catalog_handlers
from src.application.tools.order_handlers import build_order_handlers
from src.application.use_cases.dispatch_direct_action_use_case import DispatchDirectActionUseCase
from src.application.use_cases.interpret_and_respond_use_case import InterpretAndRespondUseCase
from src.application.use_cases.manage_conversation_use_case import ManageConversationUseCase
from src.modules.mod_cart.public import provide_cart_service
from src.modules.mod_catalog.application.tools import build_tool_registry
from src.modules.mod_catalog.public import (
    provide_catalog_service,
)
from src.modules.mod_checkout.public import provide_sales_port
from src.modules.mod_orders.public import provide_orders_service
from src.ports.outbound.llm_provider_port import LLMProvider
from src.ports.outbound.tool_executor_port import ToolExecutorPort
from src.shared.application.tools.tool_registry import ToolRegistry
from src.shared.domain.sensitive_data_filter import SensitiveDataFilter
from src.shared.infrastructure.config.settings import Settings, get_settings
from src.shared.infrastructure.db import connection
from src.shared.infrastructure.db.connection import get_session
from src.shared.infrastructure.di.registry import registry
from src.shared.infrastructure.fakes.fake_ventas import FakeSalesPort
from src.shared.infrastructure.http.base_client import BaseModuleClient
from src.shared.infrastructure.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)
from src.shared.infrastructure.persistence.message_postgres_adapter import MessagePostgresAdapter
from src.shared.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.shared.infrastructure.worker.outbox_worker import OutboxProcessor


def provide_chatbot_service(session: AsyncSession = Depends(get_session)) -> ManageConversationUseCase:
    return ManageConversationUseCase(
        conversations=ConversationPostgresAdapter(session),
        messages=MessagePostgresAdapter(session),
        uow=SqlAlchemyUnitOfWork(session),
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
    handlers = {}
    handlers.update(build_catalog_handlers(catalog))
    handlers.update(build_cart_handlers(cart))
    handlers.update(build_order_handlers(provide_orders_service(session)))
    return build_tool_registry(handlers)


def provide_action_dispatcher(session: AsyncSession) -> ActionDispatcher:
    return ActionDispatcher(provide_tool_registry(session))


def sanitize_sensitive_text(text: str) -> str:
    """Redact customer secrets before they are persisted or sent to the LLM."""
    return SensitiveDataFilter().redactar(text).texto


def provide_module_client(module: str, settings: Settings | None = None) -> BaseModuleClient:
    """Module client wiring (ADR-0017): base URL + timeout from Settings.

    Switching mock<->real is config-only: point *_BASE_URL at Prism/respx or
    the real module. The typed clients are built on top of this.
    """
    settings = settings or get_settings()
    wiring = {
        "security": (settings.security_base_url, settings.security_timeout_seconds),
        "products": (settings.products_base_url, settings.products_timeout_seconds),
        "sales": (settings.sales_base_url, settings.sales_timeout_seconds),
        "dispatch": (settings.dispatch_base_url, settings.dispatch_timeout_seconds),
    }
    try:
        base_url, timeout_seconds = wiring[module]
    except KeyError:
        raise ValueError(f"unknown module {module!r} (security/products/sales/dispatch)") from None
    client = BaseModuleClient(base_url=base_url, timeout_seconds=timeout_seconds)
    client.module = module
    return client


_FAKE_SALES_PORT = FakeSalesPort()
_FAKE_POSTVENTA_PORT = FakePostventaPort()


def provide_postventa_port(settings: Settings | None = None):
    """G5 M2 Postventa: in-process fake in `SALES_MODE=fake`, HTTP otherwise."""
    settings = settings or get_settings()
    if settings.module_mode("sales") == "fake":
        return _FAKE_POSTVENTA_PORT
    return PostventaClient(
        base_url=settings.postventa_base_url,
        timeout_seconds=settings.postventa_timeout_seconds,
    )


def provide_postventa_service(session: AsyncSession = Depends(get_session)):
    """Postventa service with local idempotency and customer-token strategy."""
    settings = get_settings()
    from src.adapters.outbound.persistence.claim_ref_postgres_adapter import ClaimRefPostgresAdapter
    from src.adapters.outbound.persistence.evidence_postgres_adapter import EvidencePostgresAdapter
    from src.adapters.outbound.persistence.return_ref_postgres_adapter import ReturnRefPostgresAdapter
    from src.application.use_cases.postventa_service import PostventaService

    fake = settings.module_mode("sales") == "fake"
    return PostventaService(
        postventa=provide_postventa_port(settings),
        sales=provide_sales_port(settings),
        claim_refs=None if fake else ClaimRefPostgresAdapter(session),
        return_refs=None if fake else ReturnRefPostgresAdapter(session),
        evidence=None if fake else EvidencePostgresAdapter(session),
    )


def provide_outbox_processor(session: AsyncSession) -> OutboxProcessor:
    """Outbox processor registered by `mod-checkout` (see `mod_checkout.module`)."""
    return registry.resolve("outbox_processor", session)


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
