"""Minimal dependency wiring (ADR-0002): builds use cases from a request-scoped DB session."""
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)
from src.adapters.outbound.persistence.message_postgres_adapter import MessagePostgresAdapter
from src.adapters.outbound.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.adapters.outbound.nlp.openai_nlp_adapter import OpenAILLMProvider
from src.application.use_cases.manage_conversation_use_case import ManageConversationUseCase
from src.infrastructure.config.settings import Settings, get_settings
from src.infrastructure.db.connection import get_session
from src.ports.outbound.llm_provider_port import LLMProvider


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
