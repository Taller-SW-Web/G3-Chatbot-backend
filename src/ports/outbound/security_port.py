"""Outbound port: Seguridad (SPEC-01..03). The application depends on this,
never on the concrete HTTP adapter (ADR-0017).

Only 2xx bodies come back as data. 4xx answers raise `ModuleRequestError`
with the contract `code`; transport failures raise `ModuleUnavailableError`.
"""
from typing import Any, Protocol


class SecurityPort(Protocol):
    async def login(self, email: str, password: str) -> dict[str, Any]:
        """Returns tokens or `{mfaRequerido, challengeToken, canal}`."""
        ...

    async def request_mfa(self, challenge_token: str, canal: str | None = None) -> dict[str, Any]:
        """Asks Seguridad to send the OTP over `canal` (EMAIL/SMS)."""
        ...

    async def verify_mfa(self, challenge_token: str, code: str) -> dict[str, Any]:
        """Returns the opened session tokens when the OTP is correct."""
        ...

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        """Rotates the refresh token; a reused one makes Seguridad close every session."""
        ...

    async def logout(self, refresh_token: str) -> dict[str, Any]:
        """Closes the session in Seguridad."""
        ...

    async def me(self, holder_token: str) -> dict[str, Any]:
        """Profile of the holder of `holder_token` (A15 pattern: forwards the customer token)."""
        ...
