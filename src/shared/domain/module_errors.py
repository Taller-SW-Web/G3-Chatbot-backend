"""Errors shared by every outbound module port (ADR-0017)."""

# User-facing text for an unexpected failure: never expose exception class names.
GENERIC_UNAVAILABLE_TEXT = "No pude completar la acción en este momento. Inténtalo de nuevo en unos minutos."


class ModuleUnavailableError(Exception):
    """Timeout/connection/transport error/5xx talking to a module.

    Raised by the module clients and mapped by the application to
    `SERVICIO_NO_DISPONIBLE` (503 + module name). Lives in the port layer so
    use cases and handlers depend on the contract, never on an adapter.
    """

    # `Retry-After` del módulo cuando lo envía; lo rellena el cliente HTTP.
    retry_after: str | None = None

    def __init__(self, module: str, detail: str = ""):
        self.module = module
        self.detail = detail
        super().__init__(f"{module} unavailable: {detail}" if detail else f"{module} unavailable")


class ModuleRequestError(Exception):
    """A 4xx answer from a module: the request was rejected, not a data row.

    Carries the HTTP `status` and the contract `code` (`codigo` normalized)
    so agent tools can tell "not found / not available" apart from valid
    results (SPEC-09, SPEC-10). Only 2xx bodies are returned as data.
    """

    # `Retry-After` del módulo cuando lo envía; lo rellena el cliente HTTP.
    retry_after: str | None = None

    def __init__(
        self,
        module: str,
        status: int,
        code: str,
        detail: str = "",
        *,
        intentos_restantes: int | None = None,
        errors: list[dict[str, str]] | None = None,
    ):
        self.module = module
        self.status = status
        self.code = code
        self.detail = detail
        self.intentos_restantes = intentos_restantes
        # Sanitized `errores[{campo, mensaje}]` of a 400 VALIDACION, when present.
        self.errors = errors or []
        message = f"{module} request failed: HTTP {status} {code}"
        super().__init__(f"{message}: {detail}" if detail else message)


def failure_summary(exc: BaseException, *, default_module: str) -> tuple[str, int | None, str]:
    """Module, HTTP status and contract code of a failed module call.

    A transport/5xx failure maps to the 503 the channel exposes; an unexpected
    exception keeps its class name as the code, so the log still says *why* it
    failed without ever touching the exception message or payload.
    """
    if isinstance(exc, ModuleRequestError):
        return exc.module, exc.status, exc.code
    if isinstance(exc, ModuleUnavailableError):
        return exc.module, 503, "MODULO_NO_DISPONIBLE"
    return default_module, None, type(exc).__name__
