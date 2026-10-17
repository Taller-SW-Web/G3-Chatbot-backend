"""Outbound port: local validation of a customer access token (SPEC-03 Req. 5).

The application opens a session only after the token emitted by Seguridad is
validated locally (signature, issuer, type and `CLIENTE` role). Failures raise
domain errors so callers (HTTP edge, use cases) map them to their own surface.
"""
from typing import Any, Protocol


class InvalidTokenError(Exception):
    """Malformed token, bad signature, expired, wrong issuer/type or unknown `kid`."""


class AccountNotAllowedError(Exception):
    """The token is valid but the account cannot buy from this channel (no `CLIENTE` role)."""


class AccessTokenValidatorPort(Protocol):
    async def validate_customer(self, token: str) -> dict[str, Any]:
        """Return the verified claims; raise `InvalidTokenError` / `AccountNotAllowedError`."""
        ...
