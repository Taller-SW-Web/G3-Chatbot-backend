"""Minimal dependency wiring (ADR-0002): builds use cases from a request-scoped DB session."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)
from src.adapters.outbound.persistence.message_postgres_adapter import MessagePostgresAdapter
from src.adapters.outbound.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.adapters.outbound.nlp.openai_nlp_adapter import OpenAILLMProvider
from src.adapters.outbound.tools.empty_tool_adapter import EmptyToolAdapter
from src.application.use_cases.interpret_and_respond_use_case import InterpretAndRespondUseCase
from src.application.use_cases.manage_conversation_use_case import ManageConversationUseCase
from src.domain.services.sensitive_data_filter import SensitiveDataFilter
from src.adapters.outbound.http_clients.base_client import BaseModuleClient
from src.adapters.outbound.http_clients.inventory_client import (
    INVENTORY_TIMEOUT_SECONDS,
    InventoryClient,
)
from src.adapters.outbound.http_clients.products_client import ProductsClient
from src.infrastructure.config.settings import Settings, get_settings
from src.infrastructure.db import connection
from src.infrastructure.db.connection import get_session
from src.ports.outbound.llm_provider_port import LLMProvider
from src.ports.outbound.tool_executor_port import ToolExecutorPort


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
        )
    if settings.llm_provider == "claude":
        raise NotImplementedError("LLM_PROVIDER=claude is not implemented yet")
    raise ValueError(f"Unknown LLM_PROVIDER {settings.llm_provider!r} (supported: openai)")


def provide_tool_executor() -> ToolExecutorPort:
    """TEMPORARY: no tools until the ToolRegistry of the tools module is merged and adapted to ToolExecutorPort."""
    return EmptyToolAdapter()


def sanitize_sensitive_text(text: str) -> str:
    """Redact customer secrets before they are persisted or sent to the LLM."""
    return SensitiveDataFilter().redactar(text).texto


def provide_module_client(module: str, settings: Settings | None = None) -> BaseModuleClient:
    """Module client wiring (H3-05, ADR-0017): base URL + timeout from Settings.

    Switching mock<->real is config-only: point *_BASE_URL at Prism/respx or
    the real module. H3-06/H3-07 build the typed clients on top of this.
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


def provide_products_client(settings: Settings | None = None) -> ProductsClient:
    """Typed Productos catalog client (H3-06): base URL + 4 s timeout from Settings."""
    settings = settings or get_settings()
    return ProductsClient(
        base_url=settings.products_base_url,
        timeout_seconds=settings.products_timeout_seconds,
    )


def provide_inventory_client(settings: Settings | None = None) -> InventoryClient:
    """Typed Productos inventory client (H3-06, SPEC-10): live stock, 3 s timeout."""
    settings = settings or get_settings()
    return InventoryClient(
        base_url=settings.products_base_url,
        timeout_seconds=INVENTORY_TIMEOUT_SECONDS,
    )


def build_interpret_and_respond(session: AsyncSession) -> InterpretAndRespondUseCase:
    settings = get_settings()
    return InterpretAndRespondUseCase(
        conversations=ConversationPostgresAdapter(session),
        messages=MessagePostgresAdapter(session),
        uow=SqlAlchemyUnitOfWork(session),
        llm=provide_llm_provider(settings),
        tools=provide_tool_executor(),
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
    async with connection.AsyncSessionLocal() as session:
        yield build_interpret_and_respond(session)
