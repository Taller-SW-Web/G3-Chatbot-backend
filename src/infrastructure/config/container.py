"""Minimal dependency wiring (ADR-0002): builds use cases from a request-scoped DB session."""
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.outbound.persistence.conversation_postgres_adapter import (
    ConversationPostgresAdapter,
)
from src.adapters.outbound.persistence.message_postgres_adapter import MessagePostgresAdapter
from src.adapters.outbound.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.application.use_cases.manage_conversation_use_case import ManageConversationUseCase
from src.infrastructure.db.connection import get_session


def provide_chatbot_service(session: AsyncSession = Depends(get_session)) -> ManageConversationUseCase:
    return ManageConversationUseCase(
        conversations=ConversationPostgresAdapter(session),
        messages=MessagePostgresAdapter(session),
        uow=SqlAlchemyUnitOfWork(session),
    )
