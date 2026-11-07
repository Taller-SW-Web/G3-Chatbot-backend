"""Productos catalog client (ADR-0017).

Speaks the real Productos contract (`{PRO}/api/v1`, provisional scopes A5):
search, detail, categories, brands, prices and promotions evaluation.
Only 2xx bodies are returned as data: 4xx answers raise
`ModuleRequestError` with the contract `code`, and transport failures
(timeout/connection/5xx) raise `ModuleUnavailableError` (503 + module).
External `codigo` fields normalize to `code` on the way out.

Contract reference: G6 docs a7778ec, OpenAPI 0.6.0-rc.3 and
integraciones/ALINEACION_CHATBOT.md. Catalog variants carry SKU and optional
attributes; pricing and commercial availability are separate queries.
Service 401/403 failures become upstream unavailability, never customer logout.

Open question: respx mocks live in
`tests/helpers`; the default `http://localhost:4011` has no server behind,
so deployed environments still need a mocks hosting decision (ADR-0017).
"""
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx

from src.adapters.outbound.http_clients.base_client import BaseModuleClient
from src.domain.services.product_projection import normalize_detail
from src.ports.outbound.module_errors import (
    ModuleRequestError,
    ModuleUnavailableError,
)
from src.ports.outbound.product_repository_port import (
    MAX_SKUS_PER_CALL,
    normalize_sku_list,
)
from src.ports.outbound.request_context import correlation_id_var

logger = logging.getLogger(__name__)

MAX_PRICE_DECIMALS = Decimal("0.01")

# Used when a 4xx body carries no `code`/`codigo` (or is not JSON at all).
FALLBACK_ERROR_CODES = {
    400: "VALIDACION",
    401: "NO_AUTORIZADO",
    404: "RECURSO_NO_ENCONTRADO",
}


def format_price(value: Decimal | float | int, name: str) -> str:
    """Two-decimal wire format; rejects negatives and non-numbers."""
    try:
        amount = Decimal(str(value)).quantize(MAX_PRICE_DECIMALS)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")
    return f"{amount:.2f}"


def parse_product_body(module: str, status_code: int, body: Any) -> Any:
    """Single checkpoint: return 2xx bodies, raise on 4xx, reject the rest.

    4xx reads `code` (or legacy `codigo`) from the body; missing codes fall
    back to `FALLBACK_ERROR_CODES` or `HTTP_{status_code}`. 5xx never reaches
    here (the base client maps it to `ModuleUnavailableError`). 1xx/3xx are
    unexpected (the client does not follow redirects), so they raise
    `ModuleUnavailableError`, never `ModuleRequestError`.
    """
    if 200 <= status_code < 300:
        return body
    if 400 <= status_code < 500:
        code = FALLBACK_ERROR_CODES.get(status_code, f"HTTP_{status_code}")
        if isinstance(body, dict):
            code = str(body.get("code", body.get("codigo", code)))
        raise ModuleRequestError(module, status_code, code)
    raise ModuleUnavailableError(module, f"unexpected HTTP {status_code}")


class ProductsBaseClient(BaseModuleClient):
    """Shared Productos transport: module name plus the single 2xx/4xx checkpoint."""

    module: str = "productos"

    async def _request(self, method, path, correlation_id=None, **kwargs):
        correlation_id = correlation_id or correlation_id_var.get() or str(uuid.uuid4())
        provider = getattr(self, "_service_token_provider", None)
        if provider is not None:
            headers = dict(kwargs.pop("headers", {}))
            try:
                token = await provider.get_token(correlation_id=correlation_id)
            except ModuleRequestError as exc:
                if exc.status not in (401, 403):
                    raise
                logger.warning("service credential rejected by token provider: HTTP %s", exc.status)
                raise ModuleUnavailableError(self.module, "service credential rejected") from exc
            headers["Authorization"] = "Bearer " + token
            kwargs["headers"] = headers
        response = await super()._request(method, path, correlation_id=correlation_id, **kwargs)
        if response.status_code in (401, 403):
            if response.status_code == 401 and provider is not None:
                provider.invalidate(token)
            logger.warning("service credential rejected by %s: HTTP %s", self.module, response.status_code)
            raise ModuleUnavailableError(self.module, "service credential rejected")
        return response

    async def _parse_response(self, response: httpx.Response) -> Any:
        try:
            body = response.json()
        except ValueError as exc:
            # A non-JSON 4xx (e.g. an HTML 404 from a proxy) is still a
            # rejected request: keep the status. Anything else is a broken
            # module answer.
            if 400 <= response.status_code < 500:
                body = None
            else:
                raise ModuleUnavailableError(
                    self.module, f"invalid JSON body: {exc}"
                ) from exc
        try:
            parsed = parse_product_body(self.module, response.status_code, body)
        except ModuleRequestError as exc:
            exc.retry_after = response.headers.get("Retry-After")
            raise
        return self.normalize_code(parsed) if isinstance(parsed, dict) else parsed


class ProductsClient(ProductsBaseClient):
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 4.0,
        client: httpx.AsyncClient | None = None,
        service_token_provider=None,
    ):
        super().__init__(base_url=base_url, timeout_seconds=timeout_seconds, client=client)
        self._service_token_provider = service_token_provider

    async def search_products(
        self,
        *,
        q: str | None = None,
        category_id: str | None = None,
        brand_id: str | None = None,
        channel: str = "CHATBOT",
        page: int = 1,
        size: int = 10,
        state: str = "ACTIVO",
        min_price: Decimal | float | int | None = None,
        max_price: Decimal | float | int | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        if page < 1:
            raise ValueError(f"page must be >= 1, got {page}")
        if not 1 <= size <= 10:
            raise ValueError(f"size must be 1..10, got {size}")
        params: dict[str, Any] = {
            "canal": channel,
            "pagina": page,
            "tamanio": size,
        }
        if q is not None:
            params["q"] = q
        if category_id is not None:
            params["categoriaId"] = category_id
        if brand_id is not None:
            params["marcaId"] = brand_id
        if min_price is not None:
            params["precioMin"] = format_price(min_price, "min_price")
        if max_price is not None:
            params["precioMax"] = format_price(max_price, "max_price")
        if min_price is not None and max_price is not None:
            if Decimal(params["precioMin"]) > Decimal(params["precioMax"]):
                raise ValueError(
                    f"min_price must be <= max_price, got {min_price!r} > {max_price!r}"
                )
        response = await self._request(
            "GET", "/productos", params=params, correlation_id=correlation_id
        )
        return await self._parse_response(response)

    async def get_product_detail(
        self, product_id: str, *, correlation_id: str | None = None
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"/productos/{quote(product_id, safe='')}",
            params={"canal": "CHATBOT"},
            correlation_id=correlation_id,
        )
        return normalize_detail(await self._parse_response(response))

    async def list_categories(
        self, *, correlation_id: str | None = None
    ) -> list[dict[str, Any]]:
        response = await self._request(
            "GET", "/categorias", correlation_id=correlation_id
        )
        return self._unwrap_list(await self._parse_response(response), "categories")

    async def list_brands(
        self, *, correlation_id: str | None = None
    ) -> list[dict[str, Any]]:
        response = await self._request("GET", "/marcas", correlation_id=correlation_id)
        return self._unwrap_list(await self._parse_response(response), "brands")

    async def get_price(
        self, sku: str, *, correlation_id: str | None = None
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"/precios/skus/{quote(sku, safe='')}",
            params={"canal": "CHATBOT"},
            correlation_id=correlation_id,
        )
        return await self._parse_response(response)

    async def get_prices(
        self,
        skus: list[str],
        *,
        channel: str = "CHATBOT",
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clean = normalize_sku_list(skus, limit=MAX_SKUS_PER_CALL)
        response = await self._request(
            "GET",
            "/precios",
            params={"skus": ",".join(clean), "canal": channel},
            correlation_id=correlation_id,
        )
        return self._unwrap_list(await self._parse_response(response))

    async def list_promotions(
        self, *, correlation_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Current promotions (`GET /promociones`): resolves `promotion_id`
        to a display name. Provisional shape until Productos confirms (A5)."""
        response = await self._request(
            "GET", "/promociones", params={"canal": "CHATBOT", "vigentes": "true"}, correlation_id=correlation_id
        )
        return self._unwrap_list(await self._parse_response(response))

    async def get_recommendations(self, product_id: str, *, correlation_id=None):
        response = await self._request("GET", "/recomendaciones",
            params={"productoId": product_id, "canal": "CHATBOT"}, correlation_id=correlation_id)
        body = await self._parse_response(response)
        if not isinstance(body, dict) or not isinstance(body.get("recommendations"), list):
            raise ModuleUnavailableError(self.module, "invalid recommendations response")
        return body["recommendations"]

    async def evaluate_promotions(
        self,
        lines: list[dict[str, Any]],
        *,
        channel_id: str = "CHATBOT",
        coupon_code: str | None = None,
        customer_ref: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "channel_id": channel_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "lines": lines,
        }
        if coupon_code is not None:
            payload["coupon_code"] = coupon_code
        if customer_ref is not None:
            payload["customer_ref"] = customer_ref
        response = await self._request(
            "POST", "/promociones/evaluar", json=payload, correlation_id=correlation_id
        )
        return await self._parse_response(response)

    @staticmethod
    def _unwrap_list(payload: Any, envelope: str = "items") -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in (envelope, "items"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        raise ValueError(f"expected a list or paginated items, got {type(payload).__name__}")
