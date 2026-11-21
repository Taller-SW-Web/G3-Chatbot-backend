"""Outbound port: private attachment storage (SPEC-23)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class AttachmentStorageError(Exception):
    """The private bucket could not be read or written."""


@dataclass(frozen=True)
class SignedUrl:
    url: str
    expires_at: datetime


class AttachmentStoragePort(Protocol):
    async def guardar(self, key: str, data: bytes, content_type: str) -> None: ...

    async def leer(self, key: str) -> bytes: ...

    async def eliminar(self, key: str) -> None: ...

    def url_firmada(self, key: str, ttl_seconds: int) -> SignedUrl: ...

    def verificar_firma(self, key: str, exp: int, sig: str) -> bool: ...

    async def listar_claves(self) -> list[str]:
        """Every stored object key, for orphan cleanup."""
        ...
