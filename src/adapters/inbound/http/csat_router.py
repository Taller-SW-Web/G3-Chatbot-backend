"""CSAT endpoint (SPEC-24, F5): the customer rates a delivered order."""
from typing import Any, Callable, Protocol

from fastapi import APIRouter, Depends, Request, Response, status

from src.adapters.inbound.http.postventa_errors import translate_postventa_error
from src.application.dto.postventa_dto import CreateCsatRequest
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.infrastructure.http.dependencies.auth_guard import require_customer
from src.shared.infrastructure.http.dependencies.jwt_validator import get_bearer_token


class CsatServicePort(Protocol):
    async def create_csat(
        self,
        *,
        customer_id: Any,
        holder_token: str,
        order_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        ...


def create_csat_router(service_provider: Callable[..., CsatServicePort]) -> APIRouter:
    router = APIRouter(prefix="/pedidos", tags=["csat"])

    @router.post("/{pedido_id}/csat", status_code=status.HTTP_201_CREATED)
    async def create_csat(
        pedido_id: str,
        payload: CreateCsatRequest,
        request: Request,
        caller: CallerIdentity = Depends(require_customer),
        service: CsatServicePort = Depends(service_provider),
    ) -> Response:
        try:
            await service.create_csat(
                customer_id=caller.customer,
                holder_token=get_bearer_token(request),
                order_id=pedido_id.strip(),
                payload=payload.model_dump(exclude_none=True),
            )
            return Response(status_code=status.HTTP_201_CREATED)
        except Exception as exc:
            raise translate_postventa_error(exc) from exc

    return router
