"""Errors shared by every outbound module port (ADR-0017)."""


class ModuleUnavailableError(Exception):
    """Timeout/connection/transport error/5xx talking to a module.

    Raised by the module clients and mapped by the application to
    `SERVICIO_NO_DISPONIBLE` (503 + module name). Lives in the port layer so
    use cases and handlers depend on the contract, never on an adapter.
    """

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

    def __init__(self, module: str, status: int, code: str, detail: str = ""):
        self.module = module
        self.status = status
        self.code = code
        self.detail = detail
        message = f"{module} request failed: HTTP {status} {code}"
        super().__init__(f"{message}: {detail}" if detail else message)
