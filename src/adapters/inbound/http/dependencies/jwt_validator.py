"""Local JWT validation against Seguridad JWKS (SPEC-03 Req. 5).

Validates the customer access token without calling Seguridad on every
request: signature (RS256), `iss=auth-service`, `tipo=acceso`, expiry and
`CLIENTE` role. The JWKS document is cached in memory and refreshed when
an unknown `kid` arrives; a cached copy keeps serving while the JWKS
endpoint is down.
"""
import time
from dataclasses import dataclass

import httpx
import jwt
from fastapi import Depends, Request

from src.adapters.inbound.http.problem import ProblemError
from src.domain.value_objects.caller_identity import CallerIdentity

EXPECTED_ISSUER = "auth-service"
EXPECTED_TOKEN_TYPE = "acceso"
REQUIRED_ROLE = "CLIENTE"
JWKS_PATH = "/auth/.well-known/jwks.json"
JWKS_TIMEOUT_SECONDS = 3.0
JWKS_TTL_SECONDS = 3600.0


@dataclass
class JwksCache:
    keys: dict = None
    fetched_at: float = 0.0

    def __post_init__(self):
        if self.keys is None:
            self.keys = {}


_CACHED_JWKS = JwksCache()


def _problem(status: int, code: str, title: str, detail: str | None = None) -> ProblemError:
    return ProblemError(status, code, title, detail)


def extract_roles(claims: dict) -> list:
    roles = claims.get("roles", claims.get("rol", []))
    if isinstance(roles, str):
        return [roles]
    return list(roles or [])


def check_claims(claims: dict) -> dict:
    """Enforces issuer, token type and role on an already verified payload."""
    if claims.get("iss") != EXPECTED_ISSUER:
        raise _problem(401, "TOKEN_INVALIDO", "Invalid token", "Unexpected token issuer.")
    if claims.get("tipo") != EXPECTED_TOKEN_TYPE:
        raise _problem(401, "TOKEN_INVALIDO", "Invalid token", "Unexpected token type.")
    if REQUIRED_ROLE not in extract_roles(claims):
        raise _problem(
            403,
            "CUENTA_NO_PERMITIDA",
            "Account not allowed",
            "Esta cuenta no puede comprar desde este canal",
        )
    if not claims.get("sub"):
        raise _problem(401, "TOKEN_INVALIDO", "Invalid token", "Token has no subject.")
    return claims


async def fetch_jwks(base_url: str, *, force_refresh: bool = False) -> dict:
    """Returns the JWKS key set, using the cache when it is fresh.

    A stale cache is still returned when the endpoint is unreachable and a
    copy with keys exists (SPEC-03 Req. 5: JWKS down with a valid cache).
    """
    now = time.monotonic()
    fresh = bool(_CACHED_JWKS.keys) and (now - _CACHED_JWKS.fetched_at) < JWKS_TTL_SECONDS
    if fresh and not force_refresh:
        return dict(_CACHED_JWKS.keys)
    url = base_url.rstrip("/") + JWKS_PATH
    try:
        async with httpx.AsyncClient(timeout=JWKS_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
            response.raise_for_status()
            document = response.json()
    except Exception:
        if _CACHED_JWKS.keys:
            return dict(_CACHED_JWKS.keys)
        raise _problem(503, "AUTENTICACION_NO_DISPONIBLE", "Auth service unavailable",
                       "JWKS endpoint is unreachable.") from None
    keys = {k["kid"]: k for k in document.get("keys", []) if k.get("kid")}
    _CACHED_JWKS.keys = keys
    _CACHED_JWKS.fetched_at = now
    return dict(keys)


def decode_with_jwks(token: str, jwks_keys: dict) -> dict:
    """Verifies signature and expiry, then enforces issuer/type/role."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise _problem(401, "TOKEN_INVALIDO", "Invalid token", "Malformed token.") from None
    kid = header.get("kid")
    key_data = jwks_keys.get(kid)
    if key_data is None:
        raise _problem(401, "TOKEN_INVALIDO", "Invalid token", "Unknown key id.")
    try:
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            issuer=EXPECTED_ISSUER,
            options={"require": ["exp", "iss", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise _problem(401, "TOKEN_INVALIDO", "Invalid token", "Token expired.") from None
    except jwt.PyJWTError:
        raise _problem(401, "TOKEN_INVALIDO", "Invalid token", "Signature validation failed.") from None
    return check_claims(claims)


async def validate_access_token(token: str, *, security_base_url: str) -> dict:
    """Validates a customer access token, refreshing the JWKS once on unknown `kid`."""
    keys = await fetch_jwks(security_base_url)
    try:
        return decode_with_jwks(token, keys)
    except ProblemError as exc:
        if exc.code == "TOKEN_INVALIDO" and exc.detail == "Unknown key id.":
            keys = await fetch_jwks(security_base_url, force_refresh=True)
            return decode_with_jwks(token, keys)
        raise


def get_bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _problem(401, "TOKEN_INVALIDO", "Invalid token", "Missing bearer token.")
    return token.strip()


async def get_current_customer(request: Request) -> CallerIdentity:
    """FastAPI dependency: authenticated customer identity (SPEC-03 Req. 5).

    The Seguridad base URL is resolved lazily from settings so tests can
    override this dependency without configuring the environment.
    """
    from src.infrastructure.config.settings import get_settings

    token = get_bearer_token(request)
    claims = await validate_access_token(token, security_base_url=get_settings().security_base_url)
    return CallerIdentity(customer_id=claims["sub"])


CurrentCustomer = Depends(get_current_customer)
