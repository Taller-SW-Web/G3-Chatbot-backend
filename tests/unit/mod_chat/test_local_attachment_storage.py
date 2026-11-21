"""Local private attachment storage and signed URLs (SPEC-23)."""
from urllib.parse import parse_qs, urlparse

import pytest

from src.shared.application.ports.attachment_storage_port import AttachmentStorageError
from src.shared.infrastructure.storage.local_attachment_storage import LocalAttachmentStorage


def storage(tmp_path):
    return LocalAttachmentStorage(
        tmp_path,
        public_base_url="http://test/api/v1",
        secret="test-secret",
    )


@pytest.mark.asyncio
async def test_save_read_and_delete(tmp_path):
    store = storage(tmp_path)
    await store.guardar("attachments/a.jpg", b"data", "image/jpeg")
    assert await store.leer("attachments/a.jpg") == b"data"
    await store.eliminar("attachments/a.jpg")
    with pytest.raises(AttachmentStorageError):
        await store.leer("attachments/a.jpg")


def test_signed_url_carries_expiry_and_valid_signature(tmp_path):
    store = storage(tmp_path)
    signed = store.url_firmada("attachments/a.jpg", 300)
    query = parse_qs(urlparse(signed.url).query)
    key, exp, sig = query["clave"][0], int(query["exp"][0]), query["sig"][0]
    assert signed.url.startswith("http://test/api/v1/chat/adjuntos/archivo?")
    assert store.verificar_firma(key, exp, sig) is True
    assert store.verificar_firma(key, exp, "0" * 64) is False


def test_expired_or_tampered_signature_is_rejected(tmp_path):
    store = storage(tmp_path)
    signed = store.url_firmada("attachments/a.jpg", 300)
    query = parse_qs(urlparse(signed.url).query)
    exp, sig = int(query["exp"][0]), query["sig"][0]
    assert store.verificar_firma("attachments/other.jpg", exp, sig) is False
    assert store.verificar_firma("attachments/a.jpg", exp - 1000, sig) is False


@pytest.mark.asyncio
async def test_lists_stored_keys_for_orphan_cleanup(tmp_path):
    store = storage(tmp_path)
    await store.guardar("attachments/a.jpg", b"a", "image/jpeg")
    await store.guardar("attachments/b_thumb.jpg", b"b", "image/jpeg")
    assert sorted(await store.listar_claves()) == [
        "attachments/a.jpg",
        "attachments/b_thumb.jpg",
    ]
