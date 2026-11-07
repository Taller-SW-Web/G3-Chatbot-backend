"""Checkout and payment endpoints (SPEC-14, contratos-integracion.md §2.5)."""
import inspect
from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status
from pydantic import BaseModel, ConfigDict

from src.adapters.inbound.http.dependencies.auth_guard import require_customer
from src.adapters.inbound.http.dependencies.jwt_validator import get_bearer_token
from src.adapters.inbound.http.problem import ProblemError
from src.application.dto.commerce_contracts_dto import CreateCheckoutRequest
from src.application.use_cases.manage_checkout_use_case import (
    CheckoutError,
    CreateCheckoutInput,
    ManageCheckoutUseCase,
    ProcessPaymentInput,
)
from src.domain.value_objects.caller_identity import CallerIdentity


async def _safe_call(obj: Any, method_name: str, *args: Any, **kwargs: Any) -> Any:
    if obj is None:
        return None
    method = getattr(obj, method_name, None)
    if not callable(method):
        return None
    res = method(*args, **kwargs)
    if inspect.isawaitable(res):
        return await res
    return res


class ProcessPaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    numeroTarjeta: str
    titular: str
    vencimiento: str
    cvv: str


def _translate_error(err: CheckoutError) -> ProblemError:
    status_map = {
        "REQUIERE_SESION": 401,
        "TOKEN_INVALIDO": 401,
        "RECURSO_NO_ENCONTRADO": 404,
        "PAGO_RECHAZADO": 402,
        "CHECKOUT_EXPIRADO": 410,
        "VALIDACION": 400,
        "DATO_INVALIDO": 400,
        "CARRITO_VACIO": 409,
        "ERROR_PROCESAMIENTO": 503,
        "SERVICIO_NO_DISPONIBLE": 503,
        "CELULAR_NO_VERIFICADO": 403,
        "CARRITO_DESACTUALIZADO": 409,
        "SIN_COBERTURA": 422,
        "MODULO_NO_DISPONIBLE": 503,
        "CONFLICTO": 409,
    }
    http_status = status_map.get(err.code, 500)
    title_map = {
        400: "Validation error",
        401: "Authentication required",
        402: "Payment rejected",
        403: "Action forbidden",
        404: "Resource not found",
        409: "Conflict",
        410: "Checkout expired",
        422: "Unprocessable entity",
        503: "Service temporarily unavailable",
    }
    title = (
        "Cart changed"
        if err.code == "CARRITO_DESACTUALIZADO"
        else title_map.get(http_status, "Internal error")
    )
    return ProblemError(
        status=http_status,
        code=err.code,
        title=title,
        detail=err.detail,
        extra=err.extra,
    )


def create_checkout_router(
    use_case_provider: Callable[..., ManageCheckoutUseCase],
) -> APIRouter:
    router = APIRouter(tags=["checkout"])

    @router.post("/checkout", status_code=status.HTTP_201_CREATED)
    async def create_checkout(
        payload: CreateCheckoutRequest,
        response: Response,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
        caller: CallerIdentity = Depends(require_customer),
        service: ManageCheckoutUseCase = Depends(use_case_provider),
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise ProblemError(
                status=400,
                code="VALIDACION",
                title="Missing header",
                detail="El encabezado Idempotency-Key es obligatorio.",
                extra={"campo": "Idempotency-Key"},
            )

        try:
            row = await service.create_checkout(
                CreateCheckoutInput(
                    customer_id=caller.customer,
                    idempotency_key=idempotency_key,
                    cart_id=payload.cartId,
                    direccion=payload.direccion,
                    contacto=payload.contacto.model_dump(exclude_none=True) if payload.contacto else None,
                )
            )
            order_id = row.summary.get("orderId") if isinstance(row.summary, dict) else None
            return {
                "id": str(row.id),
                "cartId": str(row.cart_id),
                "orderId": order_id,
                "status": row.status,
                "total": float(row.total),
                "expiresAt": row.expires_at.isoformat(),
                "intentosRestantes": max(0, 3 - (row.payment_attempts or 0)),
                "summary": row.summary,
            }
        except CheckoutError as exc:
            raise _translate_error(exc) from exc

    @router.get("/checkout/{checkout_id}")
    async def get_checkout(
        checkout_id: UUID,
        caller: CallerIdentity = Depends(require_customer),
        service: ManageCheckoutUseCase = Depends(use_case_provider),
    ) -> dict[str, Any]:
        try:
            row = await service.get_checkout(checkout_id, caller.customer)
            order_id = row.summary.get("orderId") if isinstance(row.summary, dict) else None
            return {
                "id": str(row.id),
                "cartId": str(row.cart_id),
                "orderId": order_id,
                "status": row.status,
                "total": float(row.total),
                "expiresAt": row.expires_at.isoformat(),
                "intentosRestantes": max(0, 3 - (row.payment_attempts or 0)),
                "summary": row.summary,
            }
        except CheckoutError as exc:
            raise _translate_error(exc) from exc

    @router.post("/checkout/{checkout_id}/pago")
    async def process_payment(
        checkout_id: UUID,
        payload: ProcessPaymentRequest,
        request: Request,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
        caller: CallerIdentity = Depends(require_customer),
        service: ManageCheckoutUseCase = Depends(use_case_provider),
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise ProblemError(
                status=400,
                code="VALIDACION",
                title="Missing header",
                detail="El encabezado Idempotency-Key es obligatorio.",
                extra={"campo": "Idempotency-Key"},
            )

        token = None
        try:
            token = get_bearer_token(request)
        except Exception:
            token = None

        try:
            attempt = await service.process_payment(
                ProcessPaymentInput(
                    checkout_id=checkout_id,
                    customer_id=caller.customer,
                    idempotency_key=idempotency_key,
                    numero_tarjeta=payload.numeroTarjeta,
                    titular=payload.titular,
                    vencimiento=payload.vencimiento,
                    cvv=payload.cvv,
                    access_token=token,
                )
            )

            order_id = None
            ord_row = await _safe_call(service.orders, "obtener_por_checkout_id", checkout_id)
            if ord_row and getattr(ord_row, "order_id", None):
                order_id = ord_row.order_id

            confirmation = getattr(attempt, "confirmation", None)
            return {
                "id": str(attempt.id),
                "checkoutId": str(attempt.checkout_id),
                "orderId": order_id,
                "resultado": "APROBADO",
                "marca": attempt.brand,
                "ultimos4": attempt.last4,
                "monto": float(attempt.amount),
                "idTransaccion": attempt.transaction_id,
                "correoEnmascarado": confirmation.get("correoEnmascarado") if confirmation else None,
                "mensaje": confirmation.get("mensaje") if confirmation else None,
            }
        except CheckoutError as exc:
            raise _translate_error(exc) from exc

    return router
