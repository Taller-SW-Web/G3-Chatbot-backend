"""Pure ASGI correlation middleware, preserving streaming/disconnect behavior.

One `X-Correlation-Id` per HTTP request: a valid inbound UUID is reused, and
anything missing or malformed gets a fresh UUID. The value lives in the
request-local context (outbound module clients read it) and is copied onto the
response start message, so it reaches normal, error and streaming responses
without buffering a single body chunk.

The access log is intentionally minimal: method, path, status, duration and
correlation id. Query strings, bodies, headers and tokens never reach it.
"""
import logging
import time
import uuid

from src.shared.domain.request_context import correlation_id_var

logger = logging.getLogger(__name__)

CORRELATION_HEADER = "X-Correlation-Id"
_CORRELATION_HEADER_BYTES = CORRELATION_HEADER.lower().encode("latin-1")


def _incoming_correlation_id(scope) -> str | None:
    """First inbound header that parses as a UUID; invalid values are ignored."""
    for name, value in scope.get("headers", []):
        if name.lower() != _CORRELATION_HEADER_BYTES:
            continue
        try:
            candidate = value.decode("latin-1").strip()
            uuid.UUID(candidate)
        except (UnicodeDecodeError, ValueError, AttributeError):
            continue
        return candidate
    return None


def _with_correlation_header(message: dict, correlation_id: str) -> None:
    """Replace any upstream value so the response carries exactly one header."""
    headers = [
        (name, value)
        for name, value in message.get("headers", [])
        if name.lower() != _CORRELATION_HEADER_BYTES
    ]
    headers.append((_CORRELATION_HEADER_BYTES, correlation_id.encode("ascii")))
    message["headers"] = headers


class CorrelationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        correlation_id = _incoming_correlation_id(scope) or str(uuid.uuid4())
        token = correlation_id_var.set(correlation_id)
        # The global 500 handler runs outside this middleware (ServerErrorMiddleware),
        # so it reads the id from the ASGI state instead of the request context.
        state = scope.get("state")
        if isinstance(state, dict):
            state["correlation_id"] = correlation_id
        started = time.perf_counter()
        status = 0

        async def send_with_correlation(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                _with_correlation_header(message, correlation_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_correlation)
        except BaseException:
            # A response that never started has no status to observe; the
            # server will answer 500 after this middleware re-raises.
            status = status or 500
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.log(
                _log_level(status),
                "http_request method=%s path=%s status=%s duration_ms=%.2f correlation_id=%s",
                scope.get("method", ""),
                scope.get("path", ""),
                status,
                duration_ms,
                correlation_id,
                extra={
                    "method": scope.get("method", ""),
                    "path": scope.get("path", ""),
                    "http_status": status,
                    "duration_ms": duration_ms,
                    "correlation_id": correlation_id,
                },
            )
            correlation_id_var.reset(token)


def _log_level(status: int) -> int:
    if status >= 500:
        return logging.ERROR
    if status >= 400:
        return logging.WARNING
    return logging.INFO
