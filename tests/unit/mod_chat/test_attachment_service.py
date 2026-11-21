"""AttachmentService: ownership, limits, signed URLs, history and cleanup (SPEC-23)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.modules.mod_chat.application.attachment_service import AttachmentError, AttachmentService
from src.modules.mod_chat.ports.image_processor_port import InvalidImageError, ProcessedImage
from src.shared.application.ports.attachment_storage_port import SignedUrl
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.domain.entities.conversation import Conversation
from src.shared.domain.entities.message import Message, MessageRole

NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)
OWNER = CallerIdentity(customer_id=uuid.uuid4())
OTHER = CallerIdentity(customer_id=uuid.uuid4())
CONVERSATION = uuid.uuid4()


class FakeConversations:
    async def find_owned(self, conversation_id, owner):
        if conversation_id == CONVERSATION and owner == OWNER:
            return Conversation(id=CONVERSATION, title="t", last_message_at=NOW, summary=None)
        return None


class FakeRepo:
    def __init__(self):
        self.rows = []

    async def guardar(self, row):
        from types import SimpleNamespace

        if not hasattr(row, "id"):
            row = SimpleNamespace(
                id=uuid.uuid4(),
                conversation_id=row.conversation_id,
                message_id=None,
                status=row.status,
                storage_key=row.storage_key,
                thumbnail_key=row.thumbnail_key,
                mime_type=row.mime_type,
                size_bytes=row.size_bytes,
                width=row.width,
                height=row.height,
                created_at=NOW,
            )
        self.rows.append(row)
        return row

    async def obtener_por_id(self, attachment_id):
        return next((r for r in self.rows if r.id == attachment_id), None)

    async def contar_pendientes(self, conversation_id):
        return sum(1 for r in self.rows if r.conversation_id == conversation_id and r.status == "PENDING")

    async def listar_por_mensajes(self, message_ids):
        return [r for r in self.rows if r.message_id in message_ids]

    async def eliminar(self, attachment_id):
        self.rows = [r for r in self.rows if r.id != attachment_id]

    async def vincular_a_mensaje(self, attachment_id, message_id):
        row = await self.obtener_por_id(attachment_id)
        if row is not None and row.status == "PENDING":
            row.message_id, row.status = message_id, "SENT"
            return row
        return None

    async def listar_pendientes_antes_de(self, fecha):
        return [r for r in self.rows if r.status == "PENDING" and r.created_at < fecha]

    async def listar_claves(self):
        return {key for r in self.rows for key in (r.storage_key, r.thumbnail_key)}


class FakeStorage:
    def __init__(self):
        self.files = {}

    async def guardar(self, key, data, content_type):
        self.files[key] = data

    async def leer(self, key):
        return self.files[key]

    async def eliminar(self, key):
        self.files.pop(key, None)

    def url_firmada(self, key, ttl_seconds):
        return SignedUrl(f"https://signed/{key}", NOW + timedelta(seconds=ttl_seconds))

    def verificar_firma(self, key, exp, sig):
        return True

    async def listar_claves(self):
        return list(self.files)


class FakeProcessor:
    def procesar(self, data):
        if data == b"bad":
            raise InvalidImageError("Solo se aceptan imágenes JPG, PNG o WebP.")
        return ProcessedImage("image/png", b"normalized", b"thumb", 800, 600)


class FakeUow:
    async def commit(self):
        pass

    async def rollback(self):
        pass


def build(max_images=6):
    repo, storage = FakeRepo(), FakeStorage()
    service = AttachmentService(
        repo, FakeConversations(), storage, FakeProcessor(), FakeUow(),
        signed_url_ttl_seconds=300, max_images_per_turn=max_images, clock=lambda: NOW,
    )
    return service, repo, storage


@pytest.mark.asyncio
async def test_upload_stores_image_and_thumbnail_as_pending():
    service, repo, storage = build()
    answer = await service.upload(OWNER, CONVERSATION, b"png")
    assert answer["mimeType"] == "image/png" and answer["ancho"] == 800
    assert len(repo.rows) == 1 and repo.rows[0].status == "PENDING"
    assert set(storage.files) == {repo.rows[0].storage_key, repo.rows[0].thumbnail_key}


@pytest.mark.asyncio
async def test_foreign_conversation_is_404():
    service, _, _ = build()
    with pytest.raises(AttachmentError) as exc:
        await service.upload(OTHER, CONVERSATION, b"png")
    assert exc.value.code == "RECURSO_NO_ENCONTRADO" and exc.value.status == 404


@pytest.mark.asyncio
async def test_invalid_file_is_400():
    service, _, _ = build()
    with pytest.raises(AttachmentError) as exc:
        await service.upload(OWNER, CONVERSATION, b"bad")
    assert exc.value.code == "ADJUNTO_INVALIDO" and exc.value.status == 400


@pytest.mark.asyncio
async def test_ten_pending_attachments_is_422():
    service, repo, _ = build()
    for _ in range(10):
        await service.upload(OWNER, CONVERSATION, b"png")
    with pytest.raises(AttachmentError) as exc:
        await service.upload(OWNER, CONVERSATION, b"png")
    assert exc.value.code == "LIMITE_ADJUNTOS" and exc.value.status == 422


@pytest.mark.asyncio
async def test_sent_attachment_cannot_be_deleted():
    service, repo, _ = build()
    await service.upload(OWNER, CONVERSATION, b"png")
    row = repo.rows[0]
    row.status, row.message_id = "SENT", uuid.uuid4()
    with pytest.raises(AttachmentError) as exc:
        await service.delete(OWNER, CONVERSATION, row.id)
    assert exc.value.code == "CONFLICTO" and exc.value.status == 409


@pytest.mark.asyncio
async def test_signed_urls_require_the_owner_and_include_expiry():
    service, repo, _ = build()
    await service.upload(OWNER, CONVERSATION, b"png")
    row = repo.rows[0]
    descriptor = await service.signed_urls(OWNER, row.id)
    assert descriptor["adjuntoId"] == str(row.id)
    assert descriptor["urlMiniatura"].startswith("https://signed/")
    assert descriptor["expiraEn"] == (NOW + timedelta(seconds=300)).isoformat()
    with pytest.raises(AttachmentError):
        await service.signed_urls(OTHER, row.id)


@pytest.mark.asyncio
async def test_history_messages_carry_attachment_descriptors():
    service, repo, _ = build()
    await service.upload(OWNER, CONVERSATION, b"png")
    row = repo.rows[0]
    message = Message(
        id=uuid.uuid4(), conversation_id=CONVERSATION, role=MessageRole.CUSTOMER,
        text="", blocks=None, created_at=NOW,
    )
    await repo.vincular_a_mensaje(row.id, message.id)
    enriched = await service.attach_history([message])
    assert enriched[0].adjuntos[0]["adjuntoId"] == str(row.id)


@pytest.mark.asyncio
async def test_context_images_are_base64_and_capped():
    service, repo, _ = build(max_images=1)
    message = Message(
        id=uuid.uuid4(), conversation_id=CONVERSATION, role=MessageRole.CUSTOMER,
        text="", blocks=None, created_at=NOW,
    )
    for _ in range(2):
        await service.upload(OWNER, CONVERSATION, b"png")
        await repo.vincular_a_mensaje(repo.rows[-1].id, message.id)
    images = await service.context_images([message])
    assert len(images[message.id]) == 1
    assert images[message.id][0].data_base64


@pytest.mark.asyncio
async def test_cleanup_removes_pending_older_than_24h_and_orphans():
    service, repo, storage = build()
    await service.upload(OWNER, CONVERSATION, b"png")
    row = repo.rows[0]
    row.created_at = NOW - timedelta(hours=25)
    storage.files["attachments/orphan.jpg"] = b"x"

    removed = await service.cleanup()

    assert row.id not in {r.id for r in repo.rows}
    assert "attachments/orphan.jpg" not in storage.files
    assert removed == 2
