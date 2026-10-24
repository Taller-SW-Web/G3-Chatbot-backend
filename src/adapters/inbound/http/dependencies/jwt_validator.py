"""FastAPI dependency: validate the customer access token locally (SPEC-03 Req. 5).

The JWKS logic lives in the outbound adapter `JwksTokenValidator`; this module
only maps its domain errors to problem+json and exposes the caller identity.
The validator is a plain dependency so tests can override it and the base URL
is injected by the composition root instead of read here.
"""
import uuid
from functools import lru_cache

from fastapi import Depends, Request

from src.adapters.inbound.http.problem import ProblemError
from src.domain.value_objects.caller_identity import CallerIdentity
from src.infrastructure.config.container import provide_access_token_validator
from src.infrastructure.config.settings import get_settings
from src.ports.outbound.token_validator_port import (
    AccessTokenValidatorPort,
    AccountNotAllowedError,
    InvalidTokenError,
)


@lru_cache
def _default_validator() -> AccessTokenValidatorPort:
    # The composition root honors MODULES_MODE (fake claims or JWKS); the cache keeps the
    # JWKS key cache and the shared fake store for the whole process.
    return provide_access_token_validator(get_settings())


def get_access_token_validator() -> AccessTokenValidatorPort:
    return _default_validator()


def get_bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ProblemError(401, "TOKEN_INVALIDO", "Invalid token", "Missing bearer token.")
    return token.strip()


async def customer_from_bearer(token: str, validator: AccessTokenValidatorPort) -> CallerIdentity:
    """Validates a bearer token and maps it to the customer identity (SPEC-03 Req. 5)."""
    try:
        claims = await validator.validate_customer(token)
    except InvalidTokenError as exc:
        raise ProblemError(401, "TOKEN_INVALIDO", "Invalid token", str(exc)) from None
    except AccountNotAllowedError:
        raise ProblemError(
            403,
            "CUENTA_NO_PERMITIDA",
            "Account not allowed",
            "Esta cuenta no puede comprar desde este canal",
        ) from None
    try:
        customer_id = uuid.UUID(str(claims["sub"]))
    except (KeyError, ValueError):
        raise ProblemError(
            401, "TOKEN_INVALIDO", "Invalid token", "Token subject is missing or invalid."
        ) from None
    return CallerIdentity(customer_id=customer_id)


async def get_current_customer(
    request: Request,
    validator: AccessTokenValidatorPort = Depends(get_access_token_validator),
) -> CallerIdentity:
    """Authenticated customer identity (SPEC-03 Req. 5)."""
    return await customer_from_bearer(get_bearer_token(request), validator)


CurrentCustomer = Depends(get_current_customer)
