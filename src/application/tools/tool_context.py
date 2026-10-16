"""Execution-context helpers shared by commercial handlers (SPEC-05 Req. 6).

Identity always comes from the server-built context, never from LLM
arguments; unknown conversations resolve to None for a 404 answer.
"""
import uuid

from src.application.tools.tool_registry import ExecutionContext
from src.domain.value_objects.caller_identity import CallerIdentity


def caller_from_context(context: ExecutionContext) -> CallerIdentity | None:
    try:
        customer_id = (
            uuid.UUID(context.cliente_id) if context.cliente_id else None
        )
    except (ValueError, AttributeError):
        return None
    if customer_id is not None:
        return CallerIdentity(customer_id=customer_id)
    if context.chat_sid:
        return CallerIdentity(anonymous_sid=context.chat_sid)
    return None


def conversation_id_from_context(context: ExecutionContext) -> uuid.UUID | None:
    try:
        return uuid.UUID(context.conversacion_id) if context.conversacion_id else None
    except (ValueError, AttributeError):
        return None
