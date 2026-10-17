"""Catalog REST endpoints (SPEC-06, SPEC-09, SPEC-10, contratos §2.3).

Thin: no business rules. Anonymous reads need no session; explicit ids
work without a chat context (carousel ordinals stay unavailable then).
"""
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from src.adapters.inbound.http.problem import ProblemError
from src.application.tools.catalog_tools import (
    ProductArguments,
    SearchArguments,
)
from src.application.use_cases.manage_catalog_use_case import (
    CatalogError,
    ManageCatalogUseCase,
)


class AvailabilityOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str
    status: str
    veredicto: str | None = None


ERROR_STATUS = {
    "RECURSO_NO_ENCONTRADO": 404,
    "PRODUCTO_NO_DISPONIBLE": 409,
    "VALIDACION": 400,
    "SERVICIO_NO_DISPONIBLE": 503,
}


def _problem(error: CatalogError) -> ProblemError:
    status = ERROR_STATUS.get(error.code, 503)
    title = {
        404: "Resource not found",
        409: "Product unavailable",
        400: "Invalid request",
        503: "Service unavailable",
    }[status]
    return ProblemError(status, error.code, title, error.detail or None)


def create_catalog_router(catalog_provider) -> APIRouter:
    router = APIRouter(prefix="/catalogo", tags=["catalogo"])

    @router.get("/productos")
    async def search_products(
        q: str | None = Query(default=None),
        categoria: str | None = Query(default=None),
        marca: str | None = Query(default=None),
        precioMin: float | None = Query(default=None, ge=0),
        precioMax: float | None = Query(default=None, ge=0),
        orden: str | None = Query(default=None),
        pagina: int = Query(default=1, ge=1),
        tamanio: int = Query(default=10, ge=1, le=10),
        service: ManageCatalogUseCase = Depends(catalog_provider),
    ) -> dict[str, Any]:
        from src.application.tools.tool_registry import ExecutionContext

        _ = orden  # server-side ordering is name-only; kept for the contract shape
        try:
            result = await service.search(
                SearchArguments(
                    q=q, categoria=categoria, marca=marca,
                    precioMin=precioMin, precioMax=precioMax,
                    pagina=pagina, tamanio=tamanio,
                ),
                ExecutionContext(),
            )
        except CatalogError as exc:
            raise _problem(exc)
        bloques = result.get("bloques", [])
        items = bloques[0]["datos"]["productos"] if bloques else []
        return {"items": items, "total": result.get("total", len(items)),
                "mensaje": result.get("texto", "")}

    @router.get("/productos/{product_id}")
    async def product_detail(
        product_id: str,
        service: ManageCatalogUseCase = Depends(catalog_provider),
    ) -> dict[str, Any]:
        from src.application.tools.tool_registry import ExecutionContext

        try:
            result = await service.detail(
                ProductArguments(product_id=product_id), ExecutionContext()
            )
        except CatalogError as exc:
            raise _problem(exc)
        bloques = result.get("bloques", [])
        if not bloques:
            raise ProblemError(404, "PRODUCTO_NO_DISPONIBLE",
                               "Product unavailable", result.get("texto"))
        return bloques[0]["datos"]["producto"]

    @router.get("/disponibilidad", response_model=list[AvailabilityOut])
    async def availability(
        sku: str = Query(),
        cantidad: int | None = Query(default=None, ge=1),
        service: ManageCatalogUseCase = Depends(catalog_provider),
    ) -> list[AvailabilityOut]:
        from src.application.tools.tool_registry import ExecutionContext

        try:
            rows = await service.inventory.get_availability([sku.strip()])
        except Exception as exc:
            raise _problem(_as_catalog_error(exc))
        out = [AvailabilityOut(sku=row["sku"], status=row["status"]) for row in rows]
        if cantidad is not None:
            try:
                verdicts = await service.inventory.verify_quantities(
                    [{"sku": sku.strip(), "quantity": cantidad}])
            except Exception as exc:
                raise _problem(_as_catalog_error(exc))
            by_sku = {v.get("sku"): v.get("veredicto") for v in verdicts}
            out = [row.model_copy(update={"veredicto": by_sku.get(row.sku)})
                   for row in out]
        return out

    return router


def _as_catalog_error(exc: Exception) -> CatalogError:
    from src.application.use_cases.manage_catalog_use_case import CatalogError as Error
    from src.ports.outbound.module_errors import (
        ModuleRequestError,
        ModuleUnavailableError,
    )

    if isinstance(exc, Error):
        return exc
    if isinstance(exc, ModuleRequestError):
        return Error(exc.code, exc.detail)
    if isinstance(exc, ModuleUnavailableError):
        return Error("SERVICIO_NO_DISPONIBLE", exc.module)
    return Error("SERVICIO_NO_DISPONIBLE", type(exc).__name__)
