"""Puerto de persistencia de challenges OTP (SPEC-04)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol


class PhoneOtpStorePort(Protocol):
    async def crear(
        self, customer_id: uuid.UUID, phone: str, code_hash: str, expires_at: datetime
    ): ...

    async def contar_desde(self, customer_id: uuid.UUID, since: datetime) -> int: ...

    async def primer_envio_desde(
        self, customer_id: uuid.UUID, since: datetime
    ) -> datetime | None: ...

    async def ultimo_vigente(self, customer_id: uuid.UUID, phone: str): ...

    async def invalidar_pendientes(self, customer_id: uuid.UUID, phone: str) -> None: ...

    async def incrementar_intentos(self, challenge_id: uuid.UUID) -> int: ...

    async def consumir(self, challenge_id: uuid.UUID) -> None: ...
