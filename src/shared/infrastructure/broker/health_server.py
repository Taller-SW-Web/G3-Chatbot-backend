"""Minimal HTTP healthcheck for the consumer process (ADR-0030), no web framework.

`GET /health` (or `/`) answers 200 while the provider reports healthy and 503 otherwise,
with the provider's JSON as body. Any other path is 404 and any other method 405.
"""
import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

StatusProvider = Callable[[], tuple[bool, dict[str, Any]]]

_REASONS = {200: "OK", 404: "Not Found", 405: "Method Not Allowed", 503: "Service Unavailable"}
_HEALTH_PATHS = ("/", "/health")
_READ_TIMEOUT = 5.0


def _response(status: int, body: dict[str, Any]) -> bytes:
    payload = json.dumps(body, separators=(",", ":")).encode()
    head = (
        f"HTTP/1.1 {status} {_REASONS[status]}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode() + payload


class HealthServer:
    def __init__(self, port: int, provider: StatusProvider, host: str = "0.0.0.0"):  # noqa: S104
        self._port = port
        self._host = host
        self._provider = provider
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        """Bound port (useful when started on port 0)."""
        if self._server is None or not self._server.sockets:
            return self._port
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        logger.info("event=health_server_started port=%s", self.port)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=_READ_TIMEOUT)
            parts = request_line.decode("latin-1").split()
            if len(parts) < 2:
                return
            method, target = parts[0], parts[1].split("?", 1)[0]
            if target not in _HEALTH_PATHS:
                writer.write(_response(404, {"status": "not_found"}))
            elif method not in ("GET", "HEAD"):
                writer.write(_response(405, {"status": "method_not_allowed"}))
            else:
                healthy, body = self._provider()
                writer.write(_response(200 if healthy else 503, body))
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
