"""Outbound port: Seguridad service token (A3, `/auth/token` client_credentials).

Privileged calls (payment introspection, SPEC-14) authenticate as the
`modulo-chatbot` integration, never as the customer. The provider owns the
credential lifecycle; callers only ask for a token.
"""
from typing import Protocol


class ServiceTokenProvider(Protocol):
    async def get_token(self, *, correlation_id: str | None = None) -> str:
        """Returns a cached, non-expired service access token."""
        ...
