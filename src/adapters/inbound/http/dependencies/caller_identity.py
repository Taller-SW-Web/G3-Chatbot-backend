"""Resolves who is calling (SPEC-05 Req. 1).

Anonymous visitors are identified by the `chat_sid` cookie; the stored value is its SHA-256 hex.
Authenticated access (JWT validation, H3-09) is NOT implemented here: any Authorization header is
refused with 501 so an unverified token is never trusted. H3-09 replaces `get_caller_identity`
(it is a plain FastAPI dependency, overridable) and returns CallerIdentity(customer_id=...).
"""
import hashlib

from fastapi import Request

from src.adapters.inbound.http.problem import ProblemError
from src.domain.value_objects.caller_identity import CallerIdentity

CHAT_SID_COOKIE = "chat_sid"


def hash_chat_sid(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def get_caller_identity(request: Request) -> CallerIdentity | None:
    """None means an anonymous visitor that has no `chat_sid` cookie yet."""
    if "authorization" in request.headers:
        raise ProblemError(
            501,
            "AUTENTICACION_NO_DISPONIBLE",
            "Authenticated access is not available yet",
            "Bearer token validation is not implemented; call without Authorization.",
        )
    raw = request.cookies.get(CHAT_SID_COOKIE)
    return CallerIdentity(anonymous_sid=hash_chat_sid(raw)) if raw else None
