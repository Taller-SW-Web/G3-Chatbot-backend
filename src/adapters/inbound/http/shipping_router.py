"""Shipping quote and saved addresses endpoints (`SPEC-12`, `contratos-integracion.md` §2.4.2).

Thin HTTP adapter: enforces customer session (`401 REQUIERE_SESION`), validates
the request/response shapes via `commerce_contracts_dto`, and translates domain
or upstream Despacho/Seguridad errors into `application/problem+json`.
"""
from typing import Any, Callable, Protocol

from fastapi import APIRouter, Depends, Request

from src.adapters.inbound.http.dependencies.caller_identity import get_caller_identity
from src.adapters.inbound.http.dependencies.jwt_validator import get_bearer_token
from src.adapters.inbound.http.problem import ProblemError
from src.application.dto.commerce_contracts_dto import (
    DISPATCH_TO_CHANNEL_ERROR,
    SHIPPING_ERROR_STATUS,
    SavedAddressesResponse,
    ShippingQuoteRequest,
    ShippingQuoteResponse,
)
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.outbound.module_errors import ModuleRequestError, ModuleUnavailableError


class ShippingError(Exception):
    def __init__(
        self,
        code: str,
        detail: str | None = None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail or code)
        self.code = DISPATCH_TO_CHANNEL_ERROR.get(code, code)
        self.detail = detail
        self.extra = extra or {}


class ShippingServicePort(Protocol):
    async def list_addresses(
        self, customer_id: Any, *, holder_token: str
    ) -> dict[str, Any] | list[dict[str, Any]]:
        ...

    async def quote_shipping(
        self, customer_id: Any, payload: ShippingQuoteRequest
    ) -> dict[str, Any]:
        ...


def _require_customer(caller: CallerIdentity | None) -> CallerIdentity:
    if not caller or not caller.customer_id:
        raise ProblemError(
            status=401,
            code="REQUIERE_SESION",
            title="Authentication required",
            detail="Esta acción requiere una sesión activa de cliente.",
        )
    return caller


def _translate_shipping_error(exc: Exception) -> ProblemError:
    if isinstance(exc, ProblemError):
        return exc
    if isinstance(exc, ShippingError):
        code = exc.code
        detail = exc.detail
        extra = exc.extra
    elif isinstance(exc, ModuleRequestError):
        code = DISPATCH_TO_CHANNEL_ERROR.get(exc.code, exc.code)
        detail = exc.detail
        extra = {}
    elif isinstance(exc, ModuleUnavailableError):
        return ProblemError(
            status=503,
            code="MODULO_NO_DISPONIBLE",
            title="Module unavailable",
            detail="No pude calcular el envío ahora. Intenta de nuevo en unos segundos.",
            extra={"modulo": exc.module},
        )
    else:
        code = "SERVICIO_NO_DISPONIBLE"
        detail = "No pude calcular el envío ahora."
        extra = {}

    http_status = SHIPPING_ERROR_STATUS.get(code, 503)
    title_map = {
        400: "Invalid request",
        401: "Authentication required",
        409: "Product unavailable",
        422: "Unprocessable shipping quote",
        429: "Too many requests",
        503: "Service unavailable",
    }
    return ProblemError(
        status=http_status,
        code=code,
        title=title_map.get(http_status, "Shipping error"),
        detail=detail,
        extra=extra,
    )


def create_shipping_router(
    shipping_provider: Callable[..., ShippingServicePort],
) -> APIRouter:
    router = APIRouter(tags=["envio"])

    @router.get("/direcciones", response_model=SavedAddressesResponse)
    async def list_saved_addresses(
        request: Request,
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: ShippingServicePort = Depends(shipping_provider),
    ) -> SavedAddressesResponse:
        authenticated = _require_customer(caller)
        holder_token = get_bearer_token(request)
        try:
            raw = await service.list_addresses(
                authenticated.customer_id, holder_token=holder_token
            )
        except Exception as exc:
            raise _translate_shipping_error(exc) from exc

        rows = raw.get("direcciones", []) if isinstance(raw, dict) else raw
        return SavedAddressesResponse.model_validate({"direcciones": rows or []})

    @router.post("/envio/cotizar", response_model=ShippingQuoteResponse)
    async def quote_shipping(
        body: ShippingQuoteRequest,
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: ShippingServicePort = Depends(shipping_provider),
    ) -> ShippingQuoteResponse:
        authenticated = _require_customer(caller)
        try:
            raw = await service.quote_shipping(authenticated.customer_id, body)
        except Exception as exc:
            raise _translate_shipping_error(exc) from exc

        if raw.get("coberturaDisponible") is False:
            raise ProblemError(
                status=422,
                code="SIN_COBERTURA",
                title="District without coverage",
                detail=f"Aún no llegamos a {body.distrito}.",
                extra={"distrito": body.distrito},
            )

        return ShippingQuoteResponse(
            coberturaDisponible=True,
            costoEnvio=float(raw.get("costoEnvio", 0.0)),
            moneda=str(raw.get("moneda", "PEN")),
            plazoEstimadoDias=int(
                raw.get("plazoEstimadoDias", raw.get("plazoEstimadoDiasHabiles", 1))
            ),
            fechaEstimadaEntrega=raw.get("fechaEstimadaEntrega"),
            nombreZona=raw.get("nombreZona"),
            modalidad=str(raw.get("modalidad", "DELIVERY")),
            distrito=body.distrito,
        )

    return router
