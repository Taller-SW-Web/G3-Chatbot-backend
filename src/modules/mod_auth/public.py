"""Public surface of `mod-auth`.

Nothing else in the backend imports this module's interior: the transport
contract with Seguridad lives in `src/shared/infrastructure/auth`, and the
`ultimoDocumento` projection is resolved through the `checkout_document`
capability. The phone verification service is exposed for the `verificar_celular`
chat tool (SPEC-04).
"""
from src.modules.mod_auth.application.manage_phone_verification_use_case import (
    ManagePhoneVerificationUseCase,
    PhoneVerificationError,
)
from src.modules.mod_auth.application.manage_session_use_case import (
    InvalidCredentialsError,
    ManageSessionUseCase,
    RefreshInvalidError,
)
from src.modules.mod_auth.module import provide_manage_session, provide_phone_service

__all__ = [
    "InvalidCredentialsError",
    "ManagePhoneVerificationUseCase",
    "ManageSessionUseCase",
    "PhoneVerificationError",
    "RefreshInvalidError",
    "provide_manage_session",
    "provide_phone_service",
]
