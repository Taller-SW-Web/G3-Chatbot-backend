"""Supabase Storage adapter of the attachment port (SPEC-23, ADR-0028).

Objects live in a PRIVATE bucket and are reached with the service key, which never
leaves the server. Signed URLs are the API's own HMAC URLs (see `signed_urls`): the API
verifies the signature and streams the object, so the bucket never needs public access.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from src.shared.application.ports.attachment_storage_port import (
    AttachmentStorageError,
    SignedUrl,
)
from src.shared.infrastructure.storage.signed_urls import HmacUrlSigner, validate_key

_LIST_PAGE = 100


class SupabaseAttachmentStorage:
    def __init__(
        self,
        *,
        base_url: str,
        service_key: str,
        bucket: str,
        public_base_url: str,
        secret: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock=lambda: datetime.now(timezone.utc),
    ):
        if not (base_url and service_key and bucket):
            raise ValueError("Supabase storage needs SUPABASE_URL, SUPABASE_SERVICE_KEY and a bucket")
        self._api = f"{base_url.rstrip('/')}/storage/v1"
        self._bucket = bucket
        self._headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}
        self._timeout = timeout_seconds
        self._transport = transport
        self._signer = HmacUrlSigner(public_base_url=public_base_url, secret=secret, clock=clock)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport, headers=self._headers)

    async def _request(self, method: str, url: str, action: str, **kwargs) -> httpx.Response:
        try:
            async with self._client() as client:
                return await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            # Class only: transport errors can echo the URL.
            raise AttachmentStorageError(f"cannot {action}: {type(exc).__name__}") from exc

    def _object_url(self, key: str, *, authenticated: bool = False) -> str:
        validate_key(key)
        prefix = "object/authenticated" if authenticated else "object"
        return f"{self._api}/{prefix}/{self._bucket}/{key}"

    async def guardar(self, key: str, data: bytes, content_type: str) -> None:
        res = await self._request(
            "POST",
            self._object_url(key),
            f"write {key}",
            content=data,
            headers={"Content-Type": content_type, "x-upsert": "true"},
        )
        if res.status_code not in (200, 201):
            raise AttachmentStorageError(f"cannot write {key}: HTTP {res.status_code}")

    async def leer(self, key: str) -> bytes:
        res = await self._request("GET", self._object_url(key, authenticated=True), f"read {key}")
        if res.status_code != 200:
            raise AttachmentStorageError(f"cannot read {key}: HTTP {res.status_code}")
        return res.content

    async def eliminar(self, key: str) -> None:
        res = await self._request("DELETE", self._object_url(key), f"delete {key}")
        # Deleting a missing object is a no-op, like the local adapter (`missing_ok`).
        if res.status_code not in (200, 204, 400, 404):
            raise AttachmentStorageError(f"cannot delete {key}: HTTP {res.status_code}")

    def url_firmada(self, key: str, ttl_seconds: int) -> SignedUrl:
        validate_key(key)
        return self._signer.url_firmada(key, ttl_seconds)

    def verificar_firma(self, key: str, exp: int, sig: str) -> bool:
        return self._signer.verificar_firma(key, exp, sig)

    async def listar_claves(self) -> list[str]:
        keys: list[str] = []
        pending = [""]
        while pending:
            prefix = pending.pop()
            offset = 0
            while True:
                res = await self._request(
                    "POST",
                    f"{self._api}/object/list/{self._bucket}",
                    "list objects",
                    json={"prefix": prefix, "limit": _LIST_PAGE, "offset": offset},
                )
                if res.status_code != 200:
                    raise AttachmentStorageError(f"cannot list objects: HTTP {res.status_code}")
                entries = res.json()
                for entry in entries:
                    path = f"{prefix}{entry['name']}"
                    # Folders come back without an id; objects carry one.
                    if entry.get("id") is None:
                        pending.append(f"{path}/")
                    else:
                        keys.append(path)
                if len(entries) < _LIST_PAGE:
                    break
                offset += _LIST_PAGE
        return keys
