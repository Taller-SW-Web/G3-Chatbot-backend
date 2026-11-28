"""Catalog REST endpoints (SPEC-06, SPEC-09, SPEC-10, contratos §2.3).

Thin: no business rules. Anonymous reads need no session; explicit ids
work without a chat context (carousel ordinals stay unavailable then).
"""
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from src.modules.mod_catalog import MODULE_ID
from src.modules.mod_catalog.application.manage_catalog_use_case import (
    CatalogError,
    ManageCatalogUseCase,
)
from src.modules.mod_catalog.application.tools import (
    ProductArguments,
    SearchArguments,
)
from src.shared.infrastructure.http.openapi import problem_responses
from src.shared.infrastructure.http.problem import ProblemError


class AvailabilityOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str
    status: str
    veredicto: str | None = None
    missing_data: list[str] = Field(default_factory=list)
    mensaje: str | None = None


class CatalogSearchOut(BaseModel):
    items: list[dict[str, Any]]
    total: int
    total_scope: str
    mensaje: str
    partial: bool


ERROR_STATUS = {
    "RECURSO_NO_ENCONTRADO": 404,
    "PRODUCTO_NO_DISPONIBLE": 409,
    "VALIDACION": 400,
    "SERVICIO_NO_DISPONIBLE": 503,
}


def _decimal(value: float | None) -> Decimal | None:
    """Los query params llegan como `float`; el dominio usa `Decimal`."""
    return None if value is None else Decimal(str(value))


def _problem(error: CatalogError) -> ProblemError:
    status = error.status or ERROR_STATUS.get(error.code, 503)
    title = {
        404: "Resource not found",
        409: "Product unavailable",
        400: "Invalid request",
        503: "Service unavailable",
    }.get(status, "Upstream request rejected")
    headers = [("Retry-After", error.retry_after)] if error.retry_after else []
    return ProblemError(status, error.code, title, error.detail or None, headers=headers)


def create_catalog_router(catalog_provider) -> APIRouter:
    router = APIRouter(
        prefix="/catalogo", tags=[MODULE_ID], responses=problem_responses(503)
    )

    @router.get(
        "/productos", response_model=CatalogSearchOut, responses=problem_responses(404, 409)
    )
    async def search_products(
        q: str | None = Query(default=None),
        categoria: str | None = Query(default=None),
        marca: str | None = Query(default=None),
        talla: str | None = Query(default=None),
        color: str | None = Query(default=None),
        soloOfertas: bool = Query(default=False),
        precioMin: float | None = Query(default=None, ge=0),
        precioMax: float | None = Query(default=None, ge=0),
        orden: Literal[
            "relevancia", "precio_asc", "precio_desc", "nombre_asc", "nombre_desc"
        ] | None = Query(default=None),
        pagina: int = Query(default=1, ge=1),
        tamanio: int = Query(default=10, ge=1, le=10),
        service: ManageCatalogUseCase = Depends(catalog_provider),
    ) -> dict[str, Any]:
        from src.shared.application.tools.tool_registry import ExecutionContext

        try:
            result = await service.search(
                SearchArguments(
                    q=q, categoria=categoria, marca=marca, talla=talla, color=color,
                    soloOfertas=soloOfertas,
                    precioMin=_decimal(precioMin), precioMax=_decimal(precioMax),
                    orden=orden,
                    pagina=pagina, tamanio=tamanio,
                ),
                ExecutionContext(),
            )
        except Exception as exc:
            raise _problem(_as_catalog_error(exc)) from exc
        bloques = result.get("bloques", [])
        items = bloques[0]["datos"]["productos"] if bloques else []
        return {"items": items, "total": result.get("total", len(items)),
                "total_scope": result.get("total_scope", "catalog"),
                "mensaje": result.get("texto", ""), "partial": result.get("partial", False)}

    @router.get("/promociones")
    async def promotions(service: ManageCatalogUseCase = Depends(catalog_provider)):
        try:
            return {"items": await service.promotions()}
        except Exception as exc:
            raise _problem(_as_catalog_error(exc)) from exc

    @router.get("/productos/{product_id}", responses=problem_responses(404, 409))
    async def product_detail(
        product_id: str,
        service: ManageCatalogUseCase = Depends(catalog_provider),
    ) -> dict[str, Any]:
        from src.shared.application.tools.tool_registry import ExecutionContext

        try:
            result = await service.detail(
                ProductArguments(product_id=product_id), ExecutionContext()
            )
        except CatalogError as exc:
            raise _problem(exc) from exc
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

        try:
            statuses = await service._status_by_sku([sku.strip()], strict=True)
        except Exception as exc:
            raise _problem(_as_catalog_error(exc)) from exc
        obtained = sku.strip() in statuses
        # D2: a missing row is "availability not obtained", not the contract's INDETERMINADO.
        status = statuses.get(sku.strip(), "SERVICIO_NO_DISPONIBLE")
        out = [AvailabilityOut(sku=sku.strip(), status=status,
                               missing_data=[] if obtained else ["availability"],
                               mensaje=("No pudimos consultar la disponibilidad en este momento, intenta de nuevo."
                                        if not obtained else "No se pudo confirmar el stock."
                                        if status == "INDETERMINADO" else None))]
        if cantidad is not None and obtained:
            try:
                verdicts = await service.inventory.verify_quantities(
                    [{"sku": sku.strip(), "quantity": cantidad}])
            except Exception as exc:
                raise _problem(_as_catalog_error(exc)) from exc
            # Upstream answers `result`; our REST field keeps the published name `veredicto`.
            by_sku = {v.get("sku"): v.get("result") for v in verdicts}
            out = [row.model_copy(update={"veredicto": by_sku.get(row.sku)})
                   for row in out]
        return out

    return router


def _as_catalog_error(exc: Exception) -> CatalogError:
    from src.modules.mod_catalog.application.manage_catalog_use_case import CatalogError as Error
    from src.shared.domain.module_errors import (
        ModuleRequestError,
        ModuleUnavailableError,
    )

    if isinstance(exc, Error):
        return exc
    if isinstance(exc, ModuleRequestError):
        return Error(exc.code, exc.detail, status=exc.status,
                     retry_after=getattr(exc, "retry_after", None))
    if isinstance(exc, ModuleUnavailableError):
        return Error("SERVICIO_NO_DISPONIBLE", exc.module,
                     retry_after=getattr(exc, "retry_after", None))
    return Error("SERVICIO_NO_DISPONIBLE", type(exc).__name__)
