"""Contract tests of `AttachmentStoragePort`: the local and Supabase adapters behave the same.

Supabase runs against an in-memory fake of the Storage REST API behind `httpx.MockTransport`.
"""
import json
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest

from src.shared.application.ports.attachment_storage_port import AttachmentStorageError
from src.shared.infrastructure.config.settings import Settings
from src.shared.infrastructure.storage.factory import build_attachment_storage
from src.shared.infrastructure.storage.local_attachment_storage import LocalAttachmentStorage
from src.shared.infrastructure.storage.supabase_attachment_storage import SupabaseAttachmentStorage

BASE = "https://proj.supabase.co"
BUCKET = "chat-attachments"
SERVICE_KEY = "service-key-value"


class FakeSupabase:
    """Minimal Storage API: upload, authenticated download, delete and recursive list."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.requests: list[httpx.Request] = []
        self.fail_with: int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"message": "boom"})
        path = unquote(urlparse(str(request.url)).path)
        root = "/storage/v1/"
        assert path.startswith(root)
        rest = path[len(root):]
        if request.method == "POST" and rest == f"object/list/{BUCKET}":
            return self._list(json.loads(request.content))
        for prefix in (f"object/authenticated/{BUCKET}/", f"object/{BUCKET}/"):
            if rest.startswith(prefix):
                key = rest[len(prefix):]
                break
        else:
            return httpx.Response(404)
        if request.method == "POST":
            self.objects[key] = (request.content, request.headers["content-type"])
            return httpx.Response(200, json={"Key": f"{BUCKET}/{key}"})
        if request.method == "GET":
            if key not in self.objects:
                return httpx.Response(404, json={"message": "Object not found"})
            return httpx.Response(200, content=self.objects[key][0])
        if request.method == "DELETE":
            if key not in self.objects:
                return httpx.Response(404, json={"message": "Object not found"})
            del self.objects[key]
            return httpx.Response(200, json={"message": "Successfully deleted"})
        return httpx.Response(405)

    def _list(self, body: dict) -> httpx.Response:
        prefix, offset, limit = body["prefix"], body["offset"], body["limit"]
        names: dict[str, bool] = {}
        for key in sorted(self.objects):
            if key.startswith(prefix):
                head, _, tail = key[len(prefix):].partition("/")
                names[head] = bool(tail)  # True: folder
        entries = [
            {"name": name, "id": None if is_folder else f"id-{prefix}{name}"}
            for name, is_folder in names.items()
        ]
        return httpx.Response(200, json=entries[offset:offset + limit])


def _local(tmp_path):
    return LocalAttachmentStorage(tmp_path, public_base_url="http://test/api/v1", secret="s3cret")


def _supabase(_tmp_path, fake=None):
    return SupabaseAttachmentStorage(
        base_url=BASE, service_key=SERVICE_KEY, bucket=BUCKET,
        public_base_url="http://test/api/v1", secret="s3cret",
        transport=httpx.MockTransport(fake or FakeSupabase()),
    )


@pytest.fixture(params=[_local, _supabase], ids=["local", "supabase"])
def store(request, tmp_path):
    return request.param(tmp_path)


@pytest.mark.asyncio
async def test_save_read_overwrite_and_delete(store):
    await store.guardar("attachments/a.jpg", b"one", "image/jpeg")
    assert await store.leer("attachments/a.jpg") == b"one"
    await store.guardar("attachments/a.jpg", b"two", "image/jpeg")
    assert await store.leer("attachments/a.jpg") == b"two"
    await store.eliminar("attachments/a.jpg")
    with pytest.raises(AttachmentStorageError):
        await store.leer("attachments/a.jpg")


@pytest.mark.asyncio
async def test_deleting_a_missing_key_is_a_noop(store):
    await store.eliminar("attachments/missing.jpg")


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "../etc/passwd", "a/../../b", "with space.jpg", "a?b.jpg"])
async def test_unsafe_keys_are_rejected(store, key):
    with pytest.raises(AttachmentStorageError):
        await store.guardar(key, b"x", "image/jpeg")
    with pytest.raises(AttachmentStorageError):
        await store.leer(key)


@pytest.mark.asyncio
async def test_lists_nested_keys_for_orphan_cleanup(store):
    await store.guardar("attachments/a.jpg", b"a", "image/jpeg")
    await store.guardar("attachments/b_thumb.jpg", b"b", "image/jpeg")
    await store.guardar("attachments/sub/c.png", b"c", "image/png")
    assert sorted(await store.listar_claves()) == [
        "attachments/a.jpg", "attachments/b_thumb.jpg", "attachments/sub/c.png",
    ]


def test_signed_url_round_trip_expiry_and_tamper(store):
    signed = store.url_firmada("attachments/a.jpg", 300)
    query = parse_qs(urlparse(signed.url).query)
    key, exp, sig = query["clave"][0], int(query["exp"][0]), query["sig"][0]
    assert signed.url.startswith("http://test/api/v1/chat/adjuntos/archivo?")
    assert store.verificar_firma(key, exp, sig) is True
    assert store.verificar_firma("attachments/other.jpg", exp, sig) is False
    assert store.verificar_firma(key, exp - 1000, sig) is False
    assert store.verificar_firma(key, exp, "0" * 64) is False


@pytest.mark.asyncio
async def test_supabase_sends_the_service_key_and_never_leaks_it_in_urls():
    fake = FakeSupabase()
    store = _supabase(None, fake)
    await store.guardar("attachments/a.jpg", b"x", "image/jpeg")
    await store.leer("attachments/a.jpg")
    upload, download = fake.requests
    assert upload.headers["authorization"] == f"Bearer {SERVICE_KEY}"
    assert upload.headers["apikey"] == SERVICE_KEY
    assert upload.headers["x-upsert"] == "true"
    assert upload.headers["content-type"] == "image/jpeg"
    assert "/object/authenticated/" in str(download.url)
    assert SERVICE_KEY not in store.url_firmada("attachments/a.jpg", 60).url


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 500])
async def test_supabase_http_errors_become_storage_errors(status):
    fake = FakeSupabase()
    store = _supabase(None, fake)
    fake.fail_with = status
    for call in (
        store.guardar("attachments/a.jpg", b"x", "image/jpeg"),
        store.leer("attachments/a.jpg"),
        store.eliminar("attachments/a.jpg"),
        store.listar_claves(),
    ):
        with pytest.raises(AttachmentStorageError, match=str(status)):
            await call


@pytest.mark.asyncio
async def test_supabase_transport_failure_does_not_leak_the_url():
    def boom(request):
        raise httpx.ConnectError("connect to proj.supabase.co failed")

    store = SupabaseAttachmentStorage(
        base_url=BASE, service_key=SERVICE_KEY, bucket=BUCKET,
        public_base_url="http://test/api/v1", secret="s", transport=httpx.MockTransport(boom),
    )
    with pytest.raises(AttachmentStorageError) as exc_info:
        await store.leer("attachments/a.jpg")
    assert "supabase.co" not in str(exc_info.value)
    assert "ConnectError" in str(exc_info.value)


def test_factory_defaults_to_local_and_selects_supabase():
    assert isinstance(build_attachment_storage(Settings()), LocalAttachmentStorage)
    chosen = build_attachment_storage(
        Settings(attachment_storage="supabase", supabase_url=BASE, supabase_service_key=SERVICE_KEY)
    )
    assert isinstance(chosen, SupabaseAttachmentStorage)


def test_supabase_selection_requires_credentials_and_unknown_values_fail():
    with pytest.raises(ValueError, match="SUPABASE_URL"):
        Settings(attachment_storage="supabase")
    with pytest.raises(ValueError, match="ATTACHMENT_STORAGE"):
        Settings(attachment_storage="s3")
