"""Purchase-experience rating owned by the chatbot (SPEC-24 Req. 7)."""
from __future__ import annotations

import uuid
from typing import Any

from src.shared.application.ports.order_repository_port import SalesOrderPort
from src.shared.application.unit_of_work_port import UnitOfWorkPort
from src.shared.domain.module_errors import ModuleRequestError


class PurchaseRatingError(Exception):
    def __init__(self, code: str, detail: str, *, status: int):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


class PurchaseRatingService:
    def __init__(self, ratings, sales: SalesOrderPort, uow: UnitOfWorkPort):
        self._ratings = ratings
        self._sales = sales
        self._uow = uow

    async def create(
        self,
        customer_id: uuid.UUID,
        *,
        holder_token: str,
        order_id: str,
        estrellas: int,
        comentario: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= int(estrellas) <= 5:
            raise PurchaseRatingError("VALIDACION", "La valoración debe ser de 1 a 5.", status=400)
        if comentario is not None and len(comentario) > 500:
            raise PurchaseRatingError("VALIDACION", "El comentario supera 500 caracteres.", status=400)
        try:
            await self._sales.get_customer_order(order_id, holder_token)
        except ModuleRequestError as exc:
            if exc.status in (403, 404):
                raise PurchaseRatingError(
                    "RECURSO_NO_ENCONTRADO", "No encontré ese pedido en tu cuenta.", status=404
                ) from exc
            raise
        existing = await self._ratings.obtener_por_order(order_id)
        if existing is not None:
            raise PurchaseRatingError(
                "CONFLICTO", "Ya calificaste este pedido.", status=409
            )
        from src.modules.mod_checkout.domain.purchase_rating import (
            NewPurchaseRating,
            PurchaseRatingAlreadyExists,
        )

        try:
            row = await self._ratings.guardar(
                NewPurchaseRating(
                    order_id=order_id,
                    customer_id=customer_id,
                    rating=int(estrellas),
                    comment=comentario,
                )
            )
        except PurchaseRatingAlreadyExists as exc:
            raise PurchaseRatingError(
                "CONFLICTO", "Ya calificaste este pedido.", status=409
            ) from exc
        await self._uow.commit()
        return {
            "orderId": order_id,
            "estrellas": row.rating,
            "comentario": row.comment,
            "creadoEn": row.created_at.isoformat() if row.created_at else None,
        }
