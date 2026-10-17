"""FastAPI dependency: validate the customer access token locally (SPEC-03 Req. 5).

The JWKS logic lives in the outbound adapter `JwksTokenValidator`; this module
only maps its domain errors to problem+json and exposes the caller identity.
The validator is a plain dependency so tests can override it and the base URL
is injected by the composition root instead of read here.
"""
from functools import lru_cache

from fastapi import Depends, Request

from src.adapters.inbound.http.problem import ProblemError
from src.adapters.outbound.auth.jwks_token_validator import JwksTokenValidator
from src.domain.value_objects.caller_identity import CallerIdentity
from src.infrastructure.config.settings import get_settings
from src.ports.outbound.token_validator_port import (
    AccessTokenValidatorPort,
    AccountNotAllowedError,
    InvalidTokenError,
)


@lru_cache
def _default_validator() -> JwksTokenValidator:
    return JwksTokenValidator(security_base_url=get_settings().security_base_url)


def get_access_token_validator() -> AccessTokenValidatorPort:
    return _default_validator()


def get_bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ProblemError(401, "TOKEN_INVALIDO", "Invalid token", "Missing bearer token.")
    return token.strip()


async def get_current_customer(
    request: Request,
    validator: AccessTokenValidatorPort = Depends(get_access_token_validator),
) -> CallerIdentity:
    """Authenticated customer identity (SPEC-03 Req. 5)."""
    token = get_bearer_token(request)
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
    return CallerIdentity(customer_id=claims["sub"])


CurrentCustomer = Depends(get_current_customer)
