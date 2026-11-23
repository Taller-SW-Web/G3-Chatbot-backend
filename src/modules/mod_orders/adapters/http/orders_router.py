"""Orders, dispatch tracking and confirmation resend endpoints (`SPEC-15..18`, §2.5.2).

Enforces customer authentication (`401 REQUIERE_SESION`), hides foreign orders
behind `404 RECURSO_NO_ENCONTRADO` (`SPEC-17` Req. 1), and applies the
`SPEC-18` privacy whitelist on tracking responses.
"""
from datetime import date
from typing import Any, Callable, Protocol

from fastapi import APIRouter, Depends, Query, Request, status

from src.modules.mod_orders import MODULE_ID
from src.modules.mod_orders.application.notification_service import NotificationError
from src.modules.mod_orders.domain.tracking_mapper import sanitize_tracking_payload
from src.shared.application.dto.commerce_contracts_dto import (
    ORDERS_ERROR_STATUS,
    OrderDetailResponse,
    OrderListResponse,
    OrderTrackingResponse,
    ResendConfirmationResponse,
)
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.domain.module_errors import ModuleRequestError, ModuleUnavailableError
from src.shared.infrastructure.http.dependencies.auth_guard import require_customer
from src.shared.infrastructure.http.dependencies.jwt_validator import get_bearer_token
from src.shared.infrastructure.http.problem import ProblemError, not_found


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
        desde: str | None = None,
        hasta: str | None = None,
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
    router = APIRouter(prefix="/pedidos", tags=[MODULE_ID])

    @router.get("", response_model=OrderListResponse)
    async def list_orders(
        request: Request,
        estado: str | None = Query(default=None),
        desde: date | None = Query(default=None),
        hasta: date | None = Query(default=None),
        page: int = Query(default=1, ge=1),
        pageSize: int = Query(default=10, ge=1, le=50),
        caller: CallerIdentity = Depends(require_customer),
        service: OrdersServicePort = Depends(orders_provider),
    ) -> OrderListResponse:
        if desde is not None and hasta is not None and desde > hasta:
            raise ProblemError(
                status=400,
                code="VALIDACION",
                title="Invalid request",
                detail="El rango de fechas es inválido.",
                extra={
                    "errores": [
                        {
                            "campo": "desde",
                            "mensaje": "`desde` no puede ser posterior a `hasta`.",
                        }
                    ]
                },
            )
        holder_token = get_bearer_token(request)
        try:
            raw = await service.list_orders(
                caller.customer_id,
                holder_token=holder_token,
                estado=estado,
                desde=desde.isoformat() if desde is not None else None,
                hasta=hasta.isoformat() if hasta is not None else None,
                page=page,
                page_size=pageSize,
            )
            return OrderListResponse.model_validate(raw)
        except Exception as exc:
            raise _translate_order_error(exc) from exc

    @router.get("/{pedido_id}", response_model=OrderDetailResponse)
    async def get_order_detail(
        pedido_id: str,
        request: Request,
        caller: CallerIdentity = Depends(require_customer),
        service: OrdersServicePort = Depends(orders_provider),
    ) -> OrderDetailResponse:
        holder_token = get_bearer_token(request)
        try:
            raw = await service.get_order_detail(
                caller.customer_id,
                pedido_id.strip(),
                holder_token=holder_token,
            )
            return OrderDetailResponse.model_validate(raw)
        except Exception as exc:
            raise _translate_order_error(exc) from exc

    @router.get("/{pedido_id}/seguimiento", response_model=OrderTrackingResponse)
    async def get_order_tracking(
        pedido_id: str,
        request: Request,
        caller: CallerIdentity = Depends(require_customer),
        service: OrdersServicePort = Depends(orders_provider),
    ) -> OrderTrackingResponse:
        holder_token = get_bearer_token(request)
        try:
            raw = await service.get_order_tracking(
                caller.customer_id,
                pedido_id.strip(),
                holder_token=holder_token,
            )
            whitelisted = sanitize_tracking_payload(raw, pedido_id=pedido_id.strip(), for_llm=False)
            return OrderTrackingResponse.model_validate(whitelisted)
        except Exception as exc:
            raise _translate_order_error(exc) from exc

    @router.post(
        "/{pedido_id}/reenviar-confirmacion",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=ResendConfirmationResponse,
    )
    async def resend_confirmation(
        pedido_id: str,
        request: Request,
        caller: CallerIdentity = Depends(require_customer),
        service: OrdersServicePort = Depends(orders_provider),
    ) -> ResendConfirmationResponse:
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
                caller.customer_id,
                pedido_id.strip(),
                holder_token=holder_token,
                idempotency_key=idempotency_key,
            )
            return ResendConfirmationResponse.model_validate(raw)
        except Exception as exc:
            raise _translate_order_error(exc) from exc

    return router
