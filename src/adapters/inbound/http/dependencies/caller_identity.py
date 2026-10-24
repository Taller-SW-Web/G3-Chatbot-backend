"""Resolves who is calling (SPEC-05 Req. 1, SPEC-03 Req. 5).

A valid `Authorization: Bearer <access token>` identifies the customer and wins over any cookie.
Without that header, anonymous visitors are identified by the `chat_sid` cookie; the stored value
is its SHA-256 hex. A malformed or invalid token is refused (401) and never falls back to the cookie.
"""
import hashlib

from fastapi import Depends, Request

from src.adapters.inbound.http.dependencies.jwt_validator import (
    customer_from_bearer,
    get_access_token_validator,
    get_bearer_token,
)
from src.domain.value_objects.caller_identity import CallerIdentity
from src.ports.outbound.token_validator_port import AccessTokenValidatorPort

CHAT_SID_COOKIE = "chat_sid"


def hash_chat_sid(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def get_caller_identity(
    request: Request,
    validator: AccessTokenValidatorPort = Depends(get_access_token_validator),
) -> CallerIdentity | None:
    """None means an anonymous visitor that has no `chat_sid` cookie yet."""
    if "authorization" in request.headers:
        return await customer_from_bearer(get_bearer_token(request), validator)
    raw = request.cookies.get(CHAT_SID_COOKIE)
    return CallerIdentity(anonymous_sid=hash_chat_sid(raw)) if raw else None
