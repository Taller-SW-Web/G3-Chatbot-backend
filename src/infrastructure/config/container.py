"""Minimal dependency wiring (ADR-0002): builds use cases from a request-scoped DB session."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.auth.jwks_token_validator import JwksTokenValidator
from src.adapters.outbound.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)
from src.adapters.outbound.persistence.message_postgres_adapter import MessagePostgresAdapter
from src.adapters.outbound.persistence.checkout_postgres_adapter import CheckoutPostgresAdapter
from src.adapters.outbound.persistence.local_phone_verification_postgres_adapter import (
    LocalPhoneVerificationPostgresAdapter,
)
from src.adapters.outbound.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.adapters.outbound.nlp.openai_nlp_adapter import OpenAILLMProvider
from src.adapters.outbound.tools.registry_tool_adapter import RegistryToolAdapter
from src.application.tools.catalog_tools import build_tool_registry
from src.application.use_cases.interpret_and_respond_use_case import InterpretAndRespondUseCase
from src.application.use_cases.manage_conversation_use_case import ManageConversationUseCase
from src.application.use_cases.manage_session_use_case import ManageSessionUseCase
from src.domain.services.sensitive_data_filter import SensitiveDataFilter
from src.adapters.outbound.http_clients.base_client import BaseModuleClient
from src.adapters.outbound.http_clients.inventory_client import (
    INVENTORY_TIMEOUT_SECONDS,
    InventoryClient,
)
from src.adapters.outbound.http_clients.products_client import ProductsClient
from src.adapters.outbound.http_clients.security_client import SecurityClient
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
            reasoning_effort=settings.llm_reasoning_effort,
        )
    if settings.llm_provider == "claude":
        raise NotImplementedError("LLM_PROVIDER=claude is not implemented yet")
    raise ValueError(f"Unknown LLM_PROVIDER {settings.llm_provider!r} (supported: openai)")


def provide_tool_executor() -> ToolExecutorPort:
    """Registry with only the tools that have a handler; commercial handlers are added as modules land."""
    return RegistryToolAdapter(build_tool_registry({}))


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


def provide_products_client(settings: Settings | None = None) -> ProductsClient:
    """Typed Productos catalog client: base URL + 4 s timeout from Settings."""
    settings = settings or get_settings()
    return ProductsClient(
        base_url=settings.products_base_url,
        timeout_seconds=settings.products_timeout_seconds,
    )


def provide_inventory_client(settings: Settings | None = None) -> InventoryClient:
    """Typed Productos inventory client (SPEC-10): live stock, 3 s timeout."""
    settings = settings or get_settings()
    return InventoryClient(
        base_url=settings.products_base_url,
        timeout_seconds=INVENTORY_TIMEOUT_SECONDS,
    )


def provide_security_client(
    settings: Settings | None = None, client_secret: str | None = None
) -> SecurityClient:
    """Typed Seguridad client (SPEC-01..03): base URL + 5 s timeout
    from Settings. The secret must come from SECURITY_CLIENT_SECRET (A3)."""
    settings = settings or get_settings()
    return SecurityClient(
        base_url=settings.security_base_url,
        timeout_seconds=settings.security_timeout_seconds,
        client_secret=client_secret or settings.security_client_secret,
    )


def provide_manage_session(session: AsyncSession = Depends(get_session)) -> ManageSessionUseCase:
    """Customer session service (SPEC-03): orchestration over the Seguridad port.

    The local token validator enforces the `CLIENTE` role before a session is
    opened; the persistence ports feed `celularVerificado` / `ultimoDocumento`.
    """
    settings = get_settings()
    return ManageSessionUseCase(
        security=provide_security_client(settings),
        validator=JwksTokenValidator(security_base_url=settings.security_base_url),
        phone_verifications=LocalPhoneVerificationPostgresAdapter(session),
        checkouts=CheckoutPostgresAdapter(session),
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
