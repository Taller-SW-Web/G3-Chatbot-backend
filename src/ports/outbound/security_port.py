"""Outbound port: Seguridad (SPEC-01..03). The application depends on this,
never on the concrete HTTP adapter (ADR-0017).

Only 2xx bodies come back as data. 4xx answers raise `ModuleRequestError`
with the contract `code`; transport failures raise `ModuleUnavailableError`.
"""
from typing import Any, Protocol


class SecurityPort(Protocol):
    async def register(
        self,
        email: str,
        password: str,
        phone: str,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> dict[str, Any]:
        """Creates a `PENDIENTE_VERIFICACION` account (`canalOrigen=CHATBOT`)."""
        ...

    async def password_policy(self) -> dict[str, Any]:
        """Password rules the client must render before submitting (SPEC-01 Req. 2)."""
        ...

    async def verify_email(self, token: str) -> dict[str, Any]:
        """Exchanges the single-use email link; 410 codes say expired or already used."""
        ...

    async def resend_verification(self, email: str) -> dict[str, Any]:
        """Asks Seguridad for a new link; the answer is neutral (202)."""
        ...

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
