"""Local JWT validation against the Seguridad JWKS (SPEC-03 Req. 5, ADR-0017).

Validates the customer access token without calling Seguridad on every
request: signature (RS256), `iss=auth-service`, `tipo=acceso`, expiry and
`CLIENTE` role. The JWKS document is cached in memory; it is refreshed when
an unknown `kid` arrives, at most once every `MIN_FORCE_REFRESH_SECONDS` so
an unauthenticated client cannot force a download per request. A cached copy
keeps serving while the JWKS endpoint is down; with no cache, a transport
failure surfaces as `ModuleUnavailableError` (503 at the edge).
"""
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt

from src.ports.outbound.module_errors import ModuleUnavailableError
from src.ports.outbound.token_validator_port import (
    AccessTokenValidatorPort,
    AccountNotAllowedError,
    InvalidTokenError,
)

EXPECTED_ISSUER = "auth-service"
EXPECTED_TOKEN_TYPE = "acceso"
REQUIRED_ROLE = "CLIENTE"
JWKS_PATH = "/auth/.well-known/jwks.json"
JWKS_TIMEOUT_SECONDS = 3.0
JWKS_TTL_SECONDS = 3600.0
MIN_FORCE_REFRESH_SECONDS = 30.0


class UnknownKeyIdError(Exception):
    """The token's `kid` is not in the currently loaded JWKS."""


def extract_roles(claims: dict) -> list:
    roles = claims.get("roles", claims.get("rol", []))
    if isinstance(roles, str):
        return [roles]
    return list(roles or [])


def check_claims(claims: dict) -> dict:
    """Enforces issuer, token type and `CLIENTE` role on an already verified payload."""
    if claims.get("iss") != EXPECTED_ISSUER:
        raise InvalidTokenError("Unexpected token issuer.")
    if claims.get("tipo") != EXPECTED_TOKEN_TYPE:
        raise InvalidTokenError("Unexpected token type.")
    if REQUIRED_ROLE not in extract_roles(claims):
        raise AccountNotAllowedError("Esta cuenta no puede comprar desde este canal")
    if not claims.get("sub"):
        raise InvalidTokenError("Token has no subject.")
    return claims


def decode_with_jwks(token: str, jwks_keys: dict) -> dict:
    """Verifies signature and expiry, then enforces issuer/type/role."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise InvalidTokenError("Malformed token.") from None
    key_data = jwks_keys.get(header.get("kid"))
    if key_data is None:
        raise UnknownKeyIdError("Unknown key id.")
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
        raise InvalidTokenError("Token expired.") from None
    except jwt.PyJWTError:
        raise InvalidTokenError("Signature validation failed.") from None
    return check_claims(claims)


class JwksTokenValidator(AccessTokenValidatorPort):
    def __init__(
        self,
        security_base_url: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = JWKS_TTL_SECONDS,
        min_force_refresh_seconds: float = MIN_FORCE_REFRESH_SECONDS,
    ):
        self._base_url = security_base_url.rstrip("/")
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._min_force_refresh_seconds = min_force_refresh_seconds
        self._keys: dict = {}
        self._fetched_at = 0.0
        self._last_forced_at = float("-inf")

    async def validate_customer(self, token: str) -> dict[str, Any]:
        keys = await self._fetch_jwks()
        try:
            return decode_with_jwks(token, keys)
        except UnknownKeyIdError:
            return await self._retry_on_unknown_kid(token)

    async def _retry_on_unknown_kid(self, token: str) -> dict[str, Any]:
        """Refresh the JWKS at most once per interval; otherwise the token is simply invalid."""
        now = self._clock()
        if now - self._last_forced_at < self._min_force_refresh_seconds:
            raise InvalidTokenError("Unknown key id.")
        self._last_forced_at = now
        keys = await self._fetch_jwks(force_refresh=True)
        try:
            return decode_with_jwks(token, keys)
        except UnknownKeyIdError:
            raise InvalidTokenError("Unknown key id.") from None

    async def _fetch_jwks(self, *, force_refresh: bool = False) -> dict:
        """Cached key set; a stale copy is still served when the endpoint is unreachable."""
        now = self._clock()
        fresh = bool(self._keys) and (now - self._fetched_at) < self._ttl_seconds
        if fresh and not force_refresh:
            return dict(self._keys)
        url = self._base_url + JWKS_PATH
        try:
            async with httpx.AsyncClient(timeout=JWKS_TIMEOUT_SECONDS) as client:
                response = await client.get(url)
                response.raise_for_status()
                document = response.json()
        except Exception:
            if self._keys:
                return dict(self._keys)
            raise ModuleUnavailableError("security", "JWKS endpoint is unreachable") from None
        self._keys = {k["kid"]: k for k in document.get("keys", []) if k.get("kid")}
        self._fetched_at = now
        return dict(self._keys)
