"""Canonical customer authentication guard (H4-S6, SPEC-03, SPEC-14)."""
from fastapi import Depends

from src.shared.domain.caller_identity import CallerIdentity
from src.shared.infrastructure.http.dependencies.caller_identity import get_caller_identity
from src.shared.infrastructure.http.problem import ProblemError


async def require_customer(
    caller: CallerIdentity | None = Depends(get_caller_identity),
) -> CallerIdentity:
    """Demands an authenticated customer identity.

    If the request has no Authorization header or belongs to an anonymous visitor,
    raises 401 REQUIERE_SESION. If the token is present but invalid/expired,
    get_caller_identity already raised 401 TOKEN_INVALIDO.
    """
    if not caller or not caller.customer_id:
        raise ProblemError(
            status=401,
            code="REQUIERE_SESION",
            title="Authentication required",
            detail="Esta acción requiere una sesión activa de cliente.",
        )
    return caller
