"""Puertos propios de `mod-auth` para la verificación local del celular (SPEC-04)."""
from typing import Protocol


class SmsSenderPort(Protocol):
    async def enviar(self, phone: str, code: str) -> None:
        """Deliver (or simulate) one 6-digit OTP to `phone`."""
        ...
