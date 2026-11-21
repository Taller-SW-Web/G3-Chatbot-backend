"""Local private attachment storage (SPEC-23).

The spec targets Supabase Storage; this adapter implements the same port on a
configurable local directory so development and tests work without credentials.
Signed URLs point to `GET /api/v1/chat/adjuntos/archivo` and are HMAC-protected:
the signature is the authorization, and it expires.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from src.shared.application.ports.attachment_storage_port import (
    AttachmentStorageError,
    SignedUrl,
)

_SAFE_KEY = re.compile(r"^[A-Za-z0-9/_\-.]+$")


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
        self._base = public_base_url.rstrip("/")
        self._secret = secret.encode()
        self._clock = clock

    def _path(self, key: str) -> Path:
        if not key or not _SAFE_KEY.fullmatch(key) or ".." in Path(key).parts:
            raise AttachmentStorageError("invalid storage key")
        return self._root / key

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

    def _signature(self, key: str, exp: int) -> str:
        return hmac.new(self._secret, f"{key}:{exp}".encode(), hashlib.sha256).hexdigest()

    def url_firmada(self, key: str, ttl_seconds: int) -> SignedUrl:
        expires_at = self._clock() + timedelta(seconds=ttl_seconds)
        exp = int(expires_at.timestamp())
        query = urlencode({"clave": key, "exp": exp, "sig": self._signature(key, exp)})
        return SignedUrl(f"{self._base}/chat/adjuntos/archivo?{query}", expires_at)

    def verificar_firma(self, key: str, exp: int, sig: str) -> bool:
        if exp < int(self._clock().timestamp()):
            return False
        return hmac.compare_digest(self._signature(key, exp), sig)

    async def listar_claves(self) -> list[str]:
        if not self._root.exists():
            return []
        return [
            path.relative_to(self._root).as_posix()
            for path in self._root.rglob("*")
            if path.is_file() and not path.name.endswith(".tmp")
        ]
