"""Orders, dispatch tracking and confirmation resend endpoints (`SPEC-15..18`, §2.5.2).

Enforces customer authentication (`401 REQUIERE_SESION`), hides foreign orders
behind `404 RECURSO_NO_ENCONTRADO` (`SPEC-17` Req. 1), and applies the
`SPEC-18` privacy whitelist on tracking responses.
"""
from typing import Any, Callable, Protocol

from fastapi import APIRouter, Depends, Query, Request, status

from src.adapters.inbound.http.dependencies.caller_identity import get_caller_identity
from src.adapters.inbound.http.dependencies.jwt_validator import get_bearer_token
from src.adapters.inbound.http.problem import ProblemError, not_found
from src.application.dto.commerce_contracts_dto import (
    ORDERS_ERROR_STATUS,
    OrderDetailResponse,
    OrderListResponse,
    OrderTrackingResponse,
    ResendConfirmationResponse,
)
from src.application.use_cases.notification_service import NotificationError
from src.domain.services.tracking_mapper import sanitize_tracking_payload
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.outbound.module_errors import ModuleRequestError, ModuleUnavailableError


class OrderError(Exception):
    def __init__(
        self,
        code: str,
        detail: str | None = None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.extra = extra or {}


class OrdersServicePort(Protocol):
    async def list_orders(
        self,
        customer_id: Any,
        *,
        holder_token: str,
        estado: str | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> dict[str, Any]:
        ...

    async def get_order_detail(
        self,
        customer_id: Any,
        order_id: str,
        *,
        holder_token: str,
    ) -> dict[str, Any]:
        ...

    async def get_order_tracking(
        self,
        customer_id: Any,
        order_id: str,
        *,
        holder_token: str,
    ) -> dict[str, Any]:
        ...

    async def resend_confirmation(
        self,
        customer_id: Any,
        order_id: str,
        *,
        holder_token: str,
        idempotency_key: str,
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


def _translate_order_error(exc: Exception) -> ProblemError:
    if isinstance(exc, ProblemError):
        return exc
    if isinstance(exc, OrderError):
        code = exc.code
        detail = exc.detail
        extra = exc.extra
    elif isinstance(exc, NotificationError):
        # SPEC-16 resend: RECURSO_NO_ENCONTRADO (404) or LIMITE_REENVIOS_EXCEDIDO (429).
        code = exc.code
        detail = exc.detail or None
        extra = {}
    elif isinstance(exc, ModuleRequestError):
        # SPEC-17 Req. 1: 403/404 from Ventas on order lookup maps to 404 RECURSO_NO_ENCONTRADO
        # so the channel never leaks whether an order id exists for another customer.
        if exc.status in (403, 404):
            return not_found("No encontré ese pedido en tu cuenta.")
        code = exc.code
        detail = exc.detail
        extra = {}
    elif isinstance(exc, ModuleUnavailableError):
        return ProblemError(
            status=503,
            code="MODULO_NO_DISPONIBLE",
            title="Module unavailable",
            detail="No puedo consultar tus pedidos en este momento.",
            extra={"modulo": exc.module},
        )
    else:
        code = "SERVICIO_NO_DISPONIBLE"
        detail = "No puedo consultar tus pedidos en este momento."
        extra = {}

    http_status = ORDERS_ERROR_STATUS.get(code, 503)
    title_map = {
        400: "Invalid request",
        401: "Authentication required",
        404: "Resource not found",
        429: "Too many confirmation resends",
        503: "Service unavailable",
    }
    return ProblemError(
        status=http_status,
        code=code,
        title=title_map.get(http_status, "Order error"),
        detail=detail,
        extra=extra,
    )


def create_orders_router(
    orders_provider: Callable[..., OrdersServicePort],
) -> APIRouter:
    router = APIRouter(prefix="/pedidos", tags=["pedidos"])

    @router.get("", response_model=OrderListResponse)
    async def list_orders(
        request: Request,
        estado: str | None = Query(default=None),
        page: int = Query(default=1, ge=1),
        pageSize: int = Query(default=10, ge=1, le=50),
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: OrdersServicePort = Depends(orders_provider),
    ) -> OrderListResponse:
        authenticated = _require_customer(caller)
        holder_token = get_bearer_token(request)
        try:
            raw = await service.list_orders(
                authenticated.customer_id,
                holder_token=holder_token,
                estado=estado,
                page=page,
                page_size=pageSize,
            )
        except Exception as exc:
            raise _translate_order_error(exc) from exc
        return OrderListResponse.model_validate(raw)

    @router.get("/{pedido_id}", response_model=OrderDetailResponse)
    async def get_order_detail(
        pedido_id: str,
        request: Request,
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: OrdersServicePort = Depends(orders_provider),
    ) -> OrderDetailResponse:
        authenticated = _require_customer(caller)
        holder_token = get_bearer_token(request)
        try:
            raw = await service.get_order_detail(
                authenticated.customer_id,
                pedido_id.strip(),
                holder_token=holder_token,
            )
        except Exception as exc:
            raise _translate_order_error(exc) from exc
        return OrderDetailResponse.model_validate(raw)

    @router.get("/{pedido_id}/seguimiento", response_model=OrderTrackingResponse)
    async def get_order_tracking(
        pedido_id: str,
        request: Request,
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: OrdersServicePort = Depends(orders_provider),
    ) -> OrderTrackingResponse:
        authenticated = _require_customer(caller)
        holder_token = get_bearer_token(request)
        try:
            raw = await service.get_order_tracking(
                authenticated.customer_id,
                pedido_id.strip(),
                holder_token=holder_token,
            )
        except Exception as exc:
            raise _translate_order_error(exc) from exc

        whitelisted = sanitize_tracking_payload(raw, pedido_id=pedido_id.strip(), for_llm=False)
        return OrderTrackingResponse.model_validate(whitelisted)

    @router.post(
        "/{pedido_id}/reenviar-confirmacion",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=ResendConfirmationResponse,
    )
    async def resend_confirmation(
        pedido_id: str,
        request: Request,
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: OrdersServicePort = Depends(orders_provider),
    ) -> ResendConfirmationResponse:
        authenticated = _require_customer(caller)
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not idempotency_key:
            raise ProblemError(
                status=400,
                code="VALIDACION",
                title="Invalid request",
                detail="Idempotency-Key is required.",
            )
        holder_token = get_bearer_token(request)
        try:
            raw = await service.resend_confirmation(
                authenticated.customer_id,
                pedido_id.strip(),
                holder_token=holder_token,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise _translate_order_error(exc) from exc
        return ResendConfirmationResponse.model_validate(raw)

    return router
