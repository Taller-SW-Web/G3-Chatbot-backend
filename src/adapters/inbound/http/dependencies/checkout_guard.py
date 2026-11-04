"""Dependencies for checkout endpoints (H4-S6, SPEC-14)."""
from fastapi import Depends
from src.adapters.inbound.http.dependencies.caller_identity import get_caller_identity
from src.adapters.inbound.http.problem import ProblemError
from src.domain.value_objects.caller_identity import CallerIdentity


async def require_customer(
    caller: CallerIdentity | None = Depends(get_caller_identity),
) -> CallerIdentity:
    if not caller or not caller.customer_id:
        raise ProblemError(
            status=401,
            code="REQUIERE_SESION",
            title="Authentication required",
            detail="Esta acción requiere una sesión activa de cliente.",
        )
    return caller
