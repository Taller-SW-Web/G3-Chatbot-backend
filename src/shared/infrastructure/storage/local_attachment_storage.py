"""Local private attachment storage (SPEC-23).

Development and test adapter of the storage port on a configurable local directory
(production uses `SupabaseAttachmentStorage`). Signed URLs are HMAC-protected,
see `signed_urls`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.shared.application.ports.attachment_storage_port import (
    AttachmentStorageError,
    SignedUrl,
)
from src.shared.infrastructure.storage.signed_urls import HmacUrlSigner, validate_key


class LocalAttachmentStorage:
    def __init__(
        self,
        root: str | Path,
        *,
        public_base_url: str,
        secret: str,
        clock=lambda: datetime.now(timezone.utc),
    ):
        self._root = Path(root).resolve()
        self._signer = HmacUrlSigner(public_base_url=public_base_url, secret=secret, clock=clock)

    def _path(self, key: str) -> Path:
        return self._root / validate_key(key)

    async def guardar(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        except OSError as exc:
            raise AttachmentStorageError(f"cannot write {key}: {exc}") from exc

    async def leer(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except OSError as exc:
            raise AttachmentStorageError(f"cannot read {key}: {exc}") from exc

    async def eliminar(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as exc:
            raise AttachmentStorageError(f"cannot delete {key}: {exc}") from exc

    def url_firmada(self, key: str, ttl_seconds: int) -> SignedUrl:
        return self._signer.url_firmada(key, ttl_seconds)

    def verificar_firma(self, key: str, exp: int, sig: str) -> bool:
        return self._signer.verificar_firma(key, exp, sig)

    async def listar_claves(self) -> list[str]:
        if not self._root.exists():
            return []
        return [
            path.relative_to(self._root).as_posix()
            for path in self._root.rglob("*")
            if path.is_file() and not path.name.endswith(".tmp")
        ]
