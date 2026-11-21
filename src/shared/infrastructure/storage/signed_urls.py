"""HMAC signing of attachment URLs shared by every storage adapter (SPEC-23).

Signed URLs point to the API (`GET /api/v1/chat/adjuntos/archivo`), which verifies the
signature and streams the object through the adapter, so buckets stay private and the
port can sign synchronously. The signature is the authorization, and it expires.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from src.shared.application.ports.attachment_storage_port import (
    AttachmentStorageError,
    SignedUrl,
)

_SAFE_KEY = re.compile(r"^[A-Za-z0-9/_\-.]+$")


def validate_key(key: str) -> str:
    """Reject empty, unsafe or traversal keys; returns the key unchanged."""
    if not key or not _SAFE_KEY.fullmatch(key) or ".." in key.split("/"):
        raise AttachmentStorageError("invalid storage key")
    return key


class HmacUrlSigner:
    def __init__(self, *, public_base_url: str, secret: str, clock=lambda: datetime.now(timezone.utc)):
        self._base = public_base_url.rstrip("/")
        self._secret = secret.encode()
        self._clock = clock

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
