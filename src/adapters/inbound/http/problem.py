"""application/problem+json errors with a `code` field (README, section 5)."""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.ports.outbound.module_errors import ModuleUnavailableError


class ProblemResponse(JSONResponse):
    media_type = "application/problem+json"


class ProblemError(Exception):
    def __init__(self, status: int, code: str, title: str, detail: str | None = None,
                 extra: dict | None = None, **fields):
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        # Extra body fields: either an `extra` dict or keyword fields (both accepted).
        self.extra = {**(extra or {}), **fields}


def not_found(detail: str = "The resource does not exist or is not available to you.") -> ProblemError:
    # 404 also covers "exists but belongs to someone else" (contratos-integracion.md, errors table).
    return ProblemError(404, "RECURSO_NO_ENCONTRADO", "Resource not found", detail)


def _body(status: int, code: str, title: str, detail: str | None, **extra) -> dict:
    body = {"status": status, "code": code, "title": title, **extra}
    if detail:
        body["detail"] = detail
    return body


def register_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(_: Request, exc: ProblemError) -> ProblemResponse:
        return ProblemResponse(
            _body(exc.status, exc.code, exc.title, exc.detail, **exc.extra),
            status_code=exc.status,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> ProblemResponse:
        errores = [
            {"campo": ".".join(str(p) for p in e["loc"][1:]), "mensaje": e["msg"]}
            for e in exc.errors()
        ]
        return ProblemResponse(
            _body(400, "VALIDACION", "Invalid request", None, errores=errores), status_code=400
        )

    @app.exception_handler(ModuleUnavailableError)
    async def _module_down(_: Request, exc: ModuleUnavailableError) -> ProblemResponse:
        """A module timed out / returned 5xx: the channel is degraded, not the request (ADR-0017)."""
        return ProblemResponse(
            _body(
                503,
                "MODULO_NO_DISPONIBLE",
                "Module unavailable",
                "El servicio no está disponible en este momento. Intenta de nuevo en unos segundos.",
                modulo=exc.module,
            ),
            status_code=503,
        )
