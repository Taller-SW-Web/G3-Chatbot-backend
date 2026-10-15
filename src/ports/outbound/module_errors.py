"""Errors shared by every outbound module port (H3-06, ADR-0017)."""


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
