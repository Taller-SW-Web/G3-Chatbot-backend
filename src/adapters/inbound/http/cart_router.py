"""Cart REST endpoints (SPEC-11, contratos §2.4). Thin: no business rules.

Anonymous calls scope by `conversacionId`; authenticated calls use the
customer cart. Clearing the cart requires explicit `{"confirmado": true}`
mirroring the UI confirmation (SPEC-05 Req. 6).
"""
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from src.adapters.inbound.http.dependencies.caller_identity import get_caller_identity
from src.adapters.inbound.http.problem import ProblemError
from src.application.use_cases.manage_cart_use_case import (
    CartError,
    ManageCartUseCase,
)
from src.domain.value_objects.caller_identity import CallerIdentity

ERROR_STATUS = {
    "REQUIERE_SESION": 401,
    "RECURSO_NO_ENCONTRADO": 404,
    "LINEA_NO_ENCONTRADA": 404,
    "PRODUCTO_NO_DISPONIBLE": 409,
    "STOCK_INSUFICIENTE": 409,
    "SKU_AGOTADO": 409,
    "CARRITO_DESACTUALIZADO": 409,
    "CONFLICTO": 409,
    "VARIANTE_REQUERIDA": 409,
    "LIMITE_CANTIDAD": 422,
    "LINEA_AMBIGUA": 422,
    "VALIDACION": 400,
    # Productos could not decide (`INDETERMINADO`): never treat it as stock,
    # so the caller gets 503 and can retry instead of a wrong cart change.
    "INDETERMINADO": 503,
    "SERVICIO_NO_DISPONIBLE": 503,
}


def _problem(error: CartError) -> ProblemError:
    status = ERROR_STATUS.get(error.code, 503)
    title = {
        401: "Authentication required",
        404: "Resource not found",
        409: "Cart conflict",
        422: "Unprocessable cart change",
        400: "Invalid request",
        503: "Service unavailable",
    }[status]
    body: dict[str, Any] = {"opciones": error.data["opciones"]} \
        if error.code == "LINEA_AMBIGUA" else {}
    return ProblemError(status, error.code, title, error.detail or None,
                        extra=body)


class AddItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str | None = None
    productoRef: int | None = None
    product_id: str | None = None
    talla: str | None = None
    color: str | None = None
    cantidad: int = 1


class SetQuantityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cantidad: int


class ClearCartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmado: bool = False


def create_cart_router(cart_provider) -> APIRouter:
    router = APIRouter(prefix="/carrito", tags=["carrito"])

    @router.get("")
    async def view_cart(
        conversacionId: UUID | None = Query(default=None),
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: ManageCartUseCase = Depends(cart_provider),
    ) -> dict[str, Any]:
        try:
            result = await service.view(caller, conversacionId)
        except CartError as exc:
            raise _problem(exc)
        bloques = result.get("bloques", [])
        if not bloques:
            return {"lineas": [], "subtotal": 0, "descuentos": [],
                    "costoEnvio": None, "total": 0, "avisos": []}
        return bloques[0]["datos"]

    @router.post("/items")
    async def add_item(
        body: AddItemIn,
        conversacionId: UUID | None = Query(default=None),
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: ManageCartUseCase = Depends(cart_provider),
    ) -> dict[str, Any]:
        try:
            result = await service.add(
                caller, conversacionId, sku=body.sku,
                producto_ref=body.productoRef, product_id=body.product_id,
                size=body.talla, color=body.color, quantity=body.cantidad,
            )
        except CartError as exc:
            raise _problem(exc)
        if result.get("requiere_variante"):
            # The line needs talla/color: a problem+json, not a fake cart body.
            raise ProblemError(
                409, "VARIANTE_REQUERIDA", "Variant required",
                result.get("texto") or None,
                extra={
                    "product_id": result.get("product_id"),
                    "faltantes": result.get("faltantes", []),
                },
            )
        return {"mensaje": result["texto"], "carrito": result["bloques"][0]["datos"]}

    @router.patch("/items/{item_id}")
    async def set_quantity(
        item_id: str,
        body: SetQuantityIn,
        conversacionId: UUID | None = Query(default=None),
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: ManageCartUseCase = Depends(cart_provider),
    ) -> dict[str, Any]:
        try:
            result = await service.update_quantity(
                caller, conversacionId, item_id=item_id, quantity=body.cantidad)
        except CartError as exc:
            raise _problem(exc)
        bloques = result.get("bloques", [])
        return {"mensaje": result["texto"],
                "carrito": bloques[0]["datos"] if bloques else None}

    @router.delete("/items/{item_id}")
    async def remove_item(
        item_id: str,
        conversacionId: UUID | None = Query(default=None),
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: ManageCartUseCase = Depends(cart_provider),
    ) -> dict[str, Any]:
        try:
            result = await service.remove_item(
                caller, conversacionId, item_id=item_id)
        except CartError as exc:
            raise _problem(exc)
        return {"mensaje": result["texto"]}

    @router.delete("")
    async def clear_cart(
        body: ClearCartIn,
        conversacionId: UUID | None = Query(default=None),
        caller: CallerIdentity | None = Depends(get_caller_identity),
        service: ManageCartUseCase = Depends(cart_provider),
    ) -> dict[str, Any]:
        if not body.confirmado:
            raise ProblemError(400, "VALIDACION", "Invalid request",
                               "clearing the cart requires {\"confirmado\": true}")
        try:
            result = await service.clear(caller, conversacionId)
        except CartError as exc:
            raise _problem(exc)
        return {"mensaje": result["texto"]}

    return router
