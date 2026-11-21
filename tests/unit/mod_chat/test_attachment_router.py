"""Attachment HTTP endpoints (SPEC-23)."""
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI

from src.modules.mod_chat.adapters.http.attachment_router import create_attachment_router
from src.modules.mod_chat.application.attachment_service import AttachmentError
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.infrastructure.http.dependencies.caller_identity import get_caller_identity
from src.shared.infrastructure.http.problem import register_problem_handlers
from src.shared.infrastructure.storage.local_attachment_storage import LocalAttachmentStorage

CONVERSATION = uuid.uuid4()
CUSTOMER = uuid.uuid4()
ADJUNTO = uuid.uuid4()


class StubService:
    def __init__(self):
        self.error = None

    async def upload(self, caller, conversation_id, content):
        if self.error:
            raise self.error
        return {
            "adjuntoId": str(ADJUNTO),
            "mimeType": "image/png",
            "tamanioBytes": len(content),
            "ancho": 800,
            "alto": 600,
        }

    async def delete(self, caller, conversation_id, attachment_id):
        if self.error:
            raise self.error

    async def signed_urls(self, caller, attachment_id):
        if self.error:
            raise self.error
        return {
            "adjuntoId": str(attachment_id),
            "mimeType": "image/png",
            "ancho": 800,
            "alto": 600,
            "urlMiniatura": "http://x/thumb",
            "expiraEn": "2026-11-21T10:05:00+00:00",
        }


def app_with(service, storage):
    app = FastAPI()
    register_problem_handlers(app)
    app.dependency_overrides[get_caller_identity] = (
        lambda: CallerIdentity(customer_id=CUSTOMER)
    )
    app.include_router(
        create_attachment_router(lambda: service, lambda: storage), prefix="/api/v1"
    )
    return app


@pytest.mark.asyncio
async def test_upload_returns_201_with_metadata(tmp_path):
    service = StubService()
    storage = LocalAttachmentStorage(tmp_path, public_base_url="http://t/api/v1", secret="s")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with(service, storage)), base_url="http://test"
    ) as client:
        res = await client.post(
            f"/api/v1/chat/conversaciones/{CONVERSATION}/adjuntos",
            files={"archivo": ("foto.png", b"png-bytes", "image/png")},
        )
    assert res.status_code == 201
    assert res.json()["adjuntoId"] == str(ADJUNTO)
    assert res.json()["ancho"] == 800


@pytest.mark.asyncio
async def test_invalid_attachment_is_400(tmp_path):
    service = StubService()
    service.error = AttachmentError("ADJUNTO_INVALIDO", "No es una imagen.", status=400)
    storage = LocalAttachmentStorage(tmp_path, public_base_url="http://t/api/v1", secret="s")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with(service, storage)), base_url="http://test"
    ) as client:
        res = await client.post(
            f"/api/v1/chat/conversaciones/{CONVERSATION}/adjuntos",
            files={"archivo": ("foto.jpg", b"MZ...", "image/jpeg")},
        )
    assert res.status_code == 400
    assert res.json()["code"] == "ADJUNTO_INVALIDO"


@pytest.mark.asyncio
async def test_delete_returns_204(tmp_path):
    storage = LocalAttachmentStorage(tmp_path, public_base_url="http://t/api/v1", secret="s")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with(StubService(), storage)), base_url="http://test"
    ) as client:
        res = await client.delete(
            f"/api/v1/chat/conversaciones/{CONVERSATION}/adjuntos/{ADJUNTO}"
        )
    assert res.status_code == 204


@pytest.mark.asyncio
async def test_signed_urls_endpoint_returns_descriptor(tmp_path):
    storage = LocalAttachmentStorage(tmp_path, public_base_url="http://t/api/v1", secret="s")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with(StubService(), storage)), base_url="http://test"
    ) as client:
        res = await client.get(f"/api/v1/chat/adjuntos/{ADJUNTO}")
    assert res.status_code == 200
    assert res.json()["urlMiniatura"] == "http://x/thumb"


@pytest.mark.asyncio
async def test_private_file_is_served_only_with_a_valid_signature(tmp_path):
    storage = LocalAttachmentStorage(tmp_path, public_base_url="http://t/api/v1", secret="s")
    await storage.guardar("attachments/a.png", b"image-bytes", "image/png")
    signed = storage.url_firmada("attachments/a.png", 300)
    path = urlparse(signed.url).path.removeprefix("/api/v1")
    query = urlparse(signed.url).query
    bad_sig = "0" * 64
    bad_query = f"clave=attachments/a.png&exp={parse_qs(query)['exp'][0]}&sig={bad_sig}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with(StubService(), storage)), base_url="http://test"
    ) as client:
        ok = await client.get(f"/api/v1{path}?{query}")
        tampered = await client.get(f"/api/v1{path}?{bad_query}")
    assert ok.status_code == 200 and ok.content == b"image-bytes"
    assert tampered.status_code == 403
