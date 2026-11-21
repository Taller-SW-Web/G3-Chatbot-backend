"""Purchase rating endpoint (SPEC-24 Req. 7), owned by checkout."""
from collections.abc import Callable

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from src.modules.mod_checkout import MODULE_ID
from src.modules.mod_checkout.application.purchase_rating_service import (
    PurchaseRatingError,
    PurchaseRatingService,
)
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.infrastructure.http.dependencies.auth_guard import require_customer
from src.shared.infrastructure.http.dependencies.jwt_validator import get_bearer_token
from src.shared.infrastructure.http.problem import ProblemError


class PurchaseRatingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estrellas: int = Field(ge=1, le=5)
    comentario: str | None = Field(default=None, max_length=500)


def create_purchase_rating_router(
    provider: Callable[..., PurchaseRatingService],
) -> APIRouter:
    router = APIRouter(tags=[MODULE_ID])

    @router.post(
        "/pedidos/{pedido_id}/valoracion-compra", status_code=status.HTTP_201_CREATED
    )
    async def create_rating(
        pedido_id: str,
        body: PurchaseRatingIn,
        request: Request,
        caller: CallerIdentity = Depends(require_customer),
        service: PurchaseRatingService = Depends(provider),
    ) -> dict:
        try:
            return await service.create(
                caller.customer,
                holder_token=get_bearer_token(request),
                order_id=pedido_id.strip(),
                estrellas=body.estrellas,
                comentario=body.comentario,
            )
        except PurchaseRatingError as exc:
            raise ProblemError(exc.status, exc.code, "Rating rejected", exc.detail) from exc

    return router
