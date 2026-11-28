"""Pure ASGI middleware requiring the shared Kong -> FastAPI secret header (#256).

When `GATEWAY_SHARED_SECRET` is configured, every HTTP request except `/health`
(kept exempt so Docker/Railway container healthchecks stay healthy) must carry a
matching `X-Gateway-Secret` header injected by Kong. Direct requests bypassing the
gateway receive `403 Forbidden` in `application/problem+json` format.
Implemented as pure ASGI so SSE streaming responses are never buffered.
"""
from __future__ import annotations

import hmac
import json

GATEWAY_SECRET_HEADER = "X-Gateway-Secret"
_GATEWAY_SECRET_HEADER_BYTES = GATEWAY_SECRET_HEADER.lower().encode("latin-1")
EXEMPT_PATHS = frozenset({"/health"})

_FORBIDDEN_BODY = json.dumps(
    {
        "type": "about:blank",
        "title": "Gateway required",
        "status": 403,
        "code": "GATEWAY_REQUIRED",
        "detail": "Direct backend access is not permitted; route requests through the API gateway.",
    },
    ensure_ascii=False,
).encode("utf-8")


def _extract_gateway_secret(scope: dict) -> bytes | None:
    for name, value in scope.get("headers", []):
        if name.lower() == _GATEWAY_SECRET_HEADER_BYTES:
            return bytes(value).strip()
    return None


class GatewaySecretMiddleware:
    def __init__(self, app, *, secret: str) -> None:
        self.app = app
        self._secret_bytes = secret.encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self._secret_bytes:
            return await self.app(scope, receive, send)

        if scope.get("path") in EXEMPT_PATHS:
            return await self.app(scope, receive, send)

        provided = _extract_gateway_secret(scope)
        if not provided or not hmac.compare_digest(provided, self._secret_bytes):
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"application/problem+json"),
                        (b"content-length", str(len(_FORBIDDEN_BODY)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _FORBIDDEN_BODY, "more_body": False})
            return

        await self.app(scope, receive, send)
