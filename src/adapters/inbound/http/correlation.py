"""Pure ASGI correlation middleware, preserving streaming/disconnect behavior."""
import uuid

from src.ports.outbound.request_context import correlation_id_var


class CorrelationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        token = correlation_id_var.set(str(uuid.uuid4()))
        try:
            await self.app(scope, receive, send)
        finally:
            correlation_id_var.reset(token)
