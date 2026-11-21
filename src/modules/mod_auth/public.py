"""Public surface of `mod-auth`.

Nothing else in the backend imports this module's interior: the transport
contract with Seguridad lives in `src/shared/infrastructure/auth`, and the
`ultimoDocumento` projection is resolved through the `checkout_document`
capability. The provider stays here so tests and the composition root can
build a session service with a request-scoped session.
"""
from src.modules.mod_auth.application.manage_session_use_case import (
    InvalidCredentialsError,
    ManageSessionUseCase,
    RefreshInvalidError,
    merge_cart_lines,
)
from src.modules.mod_auth.module import provide_manage_session

__all__ = [
    "InvalidCredentialsError",
    "ManageSessionUseCase",
    "RefreshInvalidError",
    "merge_cart_lines",
    "provide_manage_session",
]
