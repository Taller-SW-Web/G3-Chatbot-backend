"""Minimal dependency wiring (ADR-0002): builds use cases from a request-scoped DB session."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.auth.jwks_token_validator import JwksTokenValidator
from src.adapters.outbound.auth.service_token_provider import SecurityServiceTokenProvider
from src.adapters.outbound.persistence.cart_item_postgres_adapter import (
    CartItemPostgresAdapter,
)
from src.adapters.outbound.persistence.cart_postgres_adapter import CartPostgresAdapter
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
from src.application.tools.action_dispatcher import ActionDispatcher
from src.application.tools.catalog_tools import build_tool_registry
from src.application.tools.tool_registry import ToolRegistry
from src.application.use_cases.dispatch_direct_action_use_case import DispatchDirectActionUseCase
from src.application.use_cases.interpret_and_respond_use_case import InterpretAndRespondUseCase
from src.application.use_cases.manage_conversation_use_case import ManageConversationUseCase
from src.application.use_cases.manage_session_use_case import ManageSessionUseCase
from src.domain.services.sensitive_data_filter import SensitiveDataFilter
from src.adapters.outbound.fakes.fake_products import (
    FakeCatalogPort,
    FakeInventoryPort,
)
from src.adapters.outbound.fakes.fake_security import (
    FakeAccessTokenValidator,
    FakeSecurityAdapter,
    FakeServiceTokenProvider,
)
from src.adapters.inbound.http.dependencies.rate_limiter import InMemoryRateLimiter
from src.adapters.outbound.http_clients.base_client import BaseModuleClient
from src.adapters.outbound.http_clients.inventory_client import (
    INVENTORY_TIMEOUT_SECONDS,
    InventoryClient,
)
from src.adapters.outbound.http_clients.products_client import ProductsClient
from src.adapters.outbound.http_clients.security_client import SecurityClient
from src.application.tools.cart_handlers import build_cart_handlers
from src.application.tools.catalog_handlers import build_catalog_handlers
from src.application.use_cases.manage_cart_use_case import ManageCartUseCase
from src.application.use_cases.manage_catalog_use_case import (
    ManageCatalogUseCase,
    OptionCache,
)
from src.infrastructure.config.settings import Settings, get_settings
from src.infrastructure.db import connection
from src.infrastructure.db.connection import get_session
from src.ports.outbound.llm_provider_port import LLMProvider
from src.ports.outbound.tool_executor_port import ToolExecutorPort

# Categories/brands list cache shared across requests (SPEC-06): created once
# by the wiring and injected, so use cases never share a module global.
_CATALOG_OPTION_CACHE = OptionCache()

# Fake Seguridad state is a singleton on purpose: registrations must survive
# across requests so the demo flow (registro -> verificar -> login) works.
_FAKE_SECURITY = FakeSecurityAdapter()
_FAKE_TOKEN_VALIDATOR = FakeAccessTokenValidator(_FAKE_SECURITY)
_FAKE_SERVICE_TOKEN_PROVIDER = FakeServiceTokenProvider()
# One limiter per process: see its module docstring for the multi-instance caveat.
_RATE_LIMITER = InMemoryRateLimiter()


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
    return build_tool_registry(handlers)


def provide_action_dispatcher(session: AsyncSession) -> ActionDispatcher:
    return ActionDispatcher(provide_tool_registry(session))


def provide_product_catalog(settings: Settings | None = None):
    """Catalog port honoring `MODULES_MODE` (ADR-0022): in-process fakes
    for deployed environments without real modules, HTTP otherwise."""
    settings = settings or get_settings()
    if settings.modules_mode == "fake":
        return FakeCatalogPort()
    return provide_products_client(settings)


def provide_product_inventory(settings: Settings | None = None):
    """Inventory port honoring `MODULES_MODE` (ADR-0022)."""
    settings = settings or get_settings()
    if settings.modules_mode == "fake":
        return FakeInventoryPort()
    return provide_inventory_client(settings)


def provide_catalog_service(
    session: AsyncSession = Depends(get_session),
) -> ManageCatalogUseCase:
    settings = get_settings()
    return ManageCatalogUseCase(
        provide_product_catalog(settings),
        provide_product_inventory(settings),
        ConversationPostgresAdapter(session),
        SqlAlchemyUnitOfWork(session),
        option_cache=_CATALOG_OPTION_CACHE,
    )


def provide_cart_service(
    session: AsyncSession = Depends(get_session),
) -> ManageCartUseCase:
    settings = get_settings()
    return ManageCartUseCase(
        CartPostgresAdapter(session),
        CartItemPostgresAdapter(session),
        provide_product_catalog(settings),
        provide_product_inventory(settings),
        ConversationPostgresAdapter(session),
        MessagePostgresAdapter(session),
        SqlAlchemyUnitOfWork(session),
    )


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


def provide_security_port(settings: Settings | None = None):
    """Seguridad port honoring `MODULES_MODE` (ADR-0022): in-process fake or HTTP client."""
    settings = settings or get_settings()
    if settings.modules_mode == "fake":
        return _FAKE_SECURITY
    return provide_security_client(settings)


def provide_access_token_validator(settings: Settings | None = None):
    """Local token validator honoring `MODULES_MODE`: fake claims or JWKS (SPEC-03 Req. 5)."""
    settings = settings or get_settings()
    if settings.modules_mode == "fake":
        return _FAKE_TOKEN_VALIDATOR
    return JwksTokenValidator(security_base_url=settings.security_base_url)


def provide_service_token_provider(settings: Settings | None = None):
    """Service token provider for privileged calls (A3), fake or client_credentials."""
    settings = settings or get_settings()
    if settings.modules_mode == "fake":
        return _FAKE_SERVICE_TOKEN_PROVIDER
    return SecurityServiceTokenProvider(
        base_url=settings.security_base_url,
        client_id=settings.security_client_id,
        client_secret=settings.security_client_secret,
        timeout_seconds=settings.security_timeout_seconds,
    )


def provide_rate_limiter() -> InMemoryRateLimiter:
    """Process-wide limiter for the public registration endpoints (SPEC-01/02)."""
    return _RATE_LIMITER


def provide_manage_session(session: AsyncSession = Depends(get_session)) -> ManageSessionUseCase:
    """Customer session service (SPEC-03): orchestration over the Seguridad port.

    The local token validator enforces the `CLIENTE` role before a session is
    opened; the persistence ports feed `celularVerificado` / `ultimoDocumento`.
    """
    settings = get_settings()
    return ManageSessionUseCase(
        security=provide_security_port(settings),
        validator=provide_access_token_validator(settings),
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
    async with connection.AsyncSessionLocal() as session:
        yield build_interpret_and_respond(session)


@asynccontextmanager
async def provide_action_scope() -> AsyncIterator[DispatchDirectActionUseCase]:
    """One database session for a direct UI action; no LLM or SSE is opened."""
    connection.get_engine()
    async with connection.AsyncSessionLocal() as session:
        yield DispatchDirectActionUseCase(
            conversations=ConversationPostgresAdapter(session),
            messages=MessagePostgresAdapter(session),
            uow=SqlAlchemyUnitOfWork(session),
            dispatcher=provide_action_dispatcher(session),
            sanitize_text=sanitize_sensitive_text,
        )
