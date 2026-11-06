"""Async request-local correlation; never conversation/user state."""
from contextvars import ContextVar

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
