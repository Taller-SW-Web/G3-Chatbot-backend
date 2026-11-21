"""Maps Postventa failures to `application/problem+json` (G5 F3/F5/F6)."""
from src.modules.mod_postventa.application.postventa_dto import POSTVENTA_ERROR_STATUS, PostventaError
from src.shared.domain.module_errors import ModuleRequestError, ModuleUnavailableError
from src.shared.infrastructure.http.problem import ProblemError, not_found

_TITLES = {
    400: "Invalid request",
    401: "Authentication required",
    403: "Access denied",
    404: "Resource not found",
    409: "Conflict",
    422: "Unprocessable entity",
    429: "Too many requests",
    503: "Service unavailable",
}


def translate_postventa_error(exc: Exception) -> ProblemError:
    if isinstance(exc, ProblemError):
        return exc
    if isinstance(exc, PostventaError):
        code = exc.code
        detail = exc.detail
        extra = exc.extra
        http_status = POSTVENTA_ERROR_STATUS.get(code, 503)
    elif isinstance(exc, ModuleRequestError):
        if exc.status in (403, 404):
            return not_found("No encontré ese recurso en tu cuenta.")
        code = exc.code
        detail = exc.detail or None
        extra = {}
        http_status = POSTVENTA_ERROR_STATUS.get(code, exc.status if 400 <= exc.status < 500 else 503)
    elif isinstance(exc, ModuleUnavailableError):
        return ProblemError(
            status=503,
            code="MODULO_NO_DISPONIBLE",
            title="Module unavailable",
            detail="El servicio de postventa no está disponible en este momento.",
            extra={"modulo": exc.module},
        )
    else:
        code = "SERVICIO_NO_DISPONIBLE"
        detail = "No pude completar la operación de postventa."
        extra = {}
        http_status = 503

    return ProblemError(
        status=http_status,
        code=code,
        title=_TITLES.get(http_status, "Postventa error"),
        detail=detail,
        extra=extra,
    )
