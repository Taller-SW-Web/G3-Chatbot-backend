"""Base HTTP client for module integrations (H3-05, ADR-0017).

Every module client (Security, Products, Sales, Dispatch) inherits from this:
the base URL comes from Settings (mock or real, no code change), each call
carries X-Correlation-Id, unexpected failures map to 503 problem+json with
the owning module, and external `codigo` fields normalize to `code`.
"""
import uuid

import httpx


class ModuleUnavailableError(Exception):
    """Timeout/connection/transport error/5xx talking to a module. Mapped to 503 + module name."""

    def __init__(self, module: str, detail: str = ""):
        self.module = module
        self.detail = detail
        super().__init__(f"{module} unavailable: {detail}" if detail else f"{module} unavailable")


class BaseModuleClient:
    module: str = "module"

    def __init__(self, base_url: str, timeout_seconds: float = 5.0, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._owned = client is None
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=timeout_seconds)

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    def _headers(self, correlation_id: str | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"X-Correlation-Id": correlation_id or uuid.uuid4().hex}
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def normalize_code(payload: dict) -> dict:
        """External modules reply with `codigo`; the channel exposes `code`."""
        if "codigo" in payload and "code" not in payload:
            payload = {**payload, "code": payload["codigo"]}
        return payload

    async def _request(self, method: str, path: str, correlation_id: str | None = None, **kwargs) -> httpx.Response:
        headers = self._headers(correlation_id, kwargs.pop("headers", None))
        try:
            response = await self._client.request(method, path, headers=headers, **kwargs)
        except httpx.TransportError as exc:  # timeouts, connect/read/protocol errors
            raise ModuleUnavailableError(self.module, str(exc)) from exc
        if response.status_code >= 500:
            raise ModuleUnavailableError(self.module, f"HTTP {response.status_code}")
        return response
