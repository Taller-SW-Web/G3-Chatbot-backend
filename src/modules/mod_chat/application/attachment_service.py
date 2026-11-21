"""Chat attachments (SPEC-23): ownership, validation, storage and signed URLs."""
from __future__ import annotations

import base64
import logging
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from src.modules.mod_chat.ports.image_processor_port import (
    ImageProcessorPort,
    InvalidImageError,
)
from src.modules.mod_chat.ports.llm_provider_port import ImagePart
from src.shared.application.ports.attachment_storage_port import (
    AttachmentStorageError,
    AttachmentStoragePort,
)
from src.shared.application.ports.conversation_repository_port import ConversationRepositoryPort
from src.shared.application.unit_of_work_port import UnitOfWorkPort
from src.shared.domain.caller_identity import CallerIdentity
from src.shared.domain.entities.message import Message, MessageRole

logger = logging.getLogger(__name__)

MAX_PENDING_PER_CONVERSATION = 10
MAX_PER_MESSAGE = 3
PENDING_TTL = timedelta(hours=24)


class AttachmentError(Exception):
    def __init__(self, code: str, detail: str, *, status: int):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


class AttachmentService:
    def __init__(
        self,
        attachments,
        conversations: ConversationRepositoryPort,
        storage: AttachmentStoragePort,
        processor: ImageProcessorPort,
        uow: UnitOfWorkPort,
        *,
        signed_url_ttl_seconds: int = 300,
        max_images_per_turn: int = 6,
        clock=lambda: datetime.now(timezone.utc),
    ):
        self._attachments = attachments
        self._conversations = conversations
        self._storage = storage
        self._processor = processor
        self._uow = uow
        self._ttl = signed_url_ttl_seconds
        self._max_images = max_images_per_turn
        self._clock = clock

    async def _owned(self, caller: CallerIdentity | None, conversation_id: uuid.UUID) -> None:
        if caller is None or await self._conversations.find_owned(conversation_id, caller) is None:
            raise AttachmentError(
                "RECURSO_NO_ENCONTRADO",
                "La conversación no existe o no te pertenece.",
                status=404,
            )

    async def upload(
        self,
        caller: CallerIdentity | None,
        conversation_id: uuid.UUID,
        content: bytes,
    ) -> dict[str, Any]:
        await self._owned(caller, conversation_id)
        if await self._attachments.contar_pendientes(conversation_id) >= MAX_PENDING_PER_CONVERSATION:
            raise AttachmentError(
                "LIMITE_ADJUNTOS",
                "Tienes demasiados adjuntos pendientes. Envía o quita alguno.",
                status=422,
            )
        try:
            processed = self._processor.procesar(content)
        except InvalidImageError as exc:
            raise AttachmentError("ADJUNTO_INVALIDO", str(exc), status=400) from exc

        suffix = "jpg" if processed.mime_type == "image/jpeg" else processed.mime_type.split("/")[-1]
        image_key = f"attachments/{uuid.uuid4().hex}.{suffix}"
        thumb_key = f"attachments/{uuid.uuid4().hex}_thumb.{suffix}"
        try:
            await self._storage.guardar(image_key, processed.data, processed.mime_type)
            await self._storage.guardar(thumb_key, processed.thumbnail, processed.mime_type)
        except AttachmentStorageError as exc:
            logger.warning("Attachment storage failed: %s", type(exc).__name__)
            await self._safe_delete(image_key, thumb_key)
            raise AttachmentError(
                "SERVICIO_NO_DISPONIBLE",
                "No pudimos subir la imagen. Intenta de nuevo.",
                status=503,
            ) from exc

        from src.shared.infrastructure.db.models.attachment import Attachment

        row = Attachment(
            conversation_id=conversation_id,
            status="PENDING",
            storage_key=image_key,
            thumbnail_key=thumb_key,
            mime_type=processed.mime_type,
            size_bytes=len(processed.data),
            width=processed.width,
            height=processed.height,
        )
        try:
            saved = await self._attachments.guardar(row)
            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            await self._safe_delete(image_key, thumb_key)
            raise
        return {
            "adjuntoId": str(saved.id),
            "mimeType": processed.mime_type,
            "tamanioBytes": len(processed.data),
            "ancho": processed.width,
            "alto": processed.height,
        }

    async def delete(
        self, caller: CallerIdentity | None, conversation_id: uuid.UUID, attachment_id: uuid.UUID
    ) -> None:
        await self._owned(caller, conversation_id)
        row = await self._attachments.obtener_por_id(attachment_id)
        if row is None or row.conversation_id != conversation_id:
            raise AttachmentError("RECURSO_NO_ENCONTRADO", "Adjunto no encontrado.", status=404)
        if row.status == "SENT":
            raise AttachmentError(
                "CONFLICTO", "Un adjunto enviado no se puede quitar.", status=409
            )
        keys = (row.storage_key, row.thumbnail_key)
        await self._attachments.eliminar(row.id)
        await self._uow.commit()
        await self._safe_delete(*keys)

    async def signed_urls(
        self, caller: CallerIdentity | None, attachment_id: uuid.UUID
    ) -> dict[str, Any]:
        row = await self._attachments.obtener_por_id(attachment_id)
        if row is None:
            raise AttachmentError("RECURSO_NO_ENCONTRADO", "Adjunto no encontrado.", status=404)
        await self._owned(caller, row.conversation_id)
        return self._descriptor(row)

    def _descriptor(self, row) -> dict[str, Any]:
        thumbnail = self._storage.url_firmada(row.thumbnail_key, self._ttl)
        image = self._storage.url_firmada(row.storage_key, self._ttl)
        return {
            "adjuntoId": str(row.id),
            "mimeType": row.mime_type,
            "ancho": row.width,
            "alto": row.height,
            "urlMiniatura": thumbnail.url,
            "expiraEn": thumbnail.expires_at.isoformat(),
            "urlImagen": image.url,
        }

    async def validate_for_message(
        self,
        caller: CallerIdentity | None,
        conversation_id: uuid.UUID,
        attachment_ids: Sequence[uuid.UUID],
    ) -> list[Any]:
        await self._owned(caller, conversation_id)
        if len(attachment_ids) > MAX_PER_MESSAGE:
            raise AttachmentError(
                "LIMITE_ADJUNTOS",
                f"Puedes adjuntar hasta {MAX_PER_MESSAGE} imágenes por mensaje.",
                status=422,
            )
        rows = []
        for attachment_id in attachment_ids:
            row = await self._attachments.obtener_por_id(attachment_id)
            if (
                row is None
                or row.conversation_id != conversation_id
                or row.status != "PENDING"
            ):
                raise AttachmentError(
                    "RECURSO_NO_ENCONTRADO",
                    "Uno de los adjuntos no existe o ya fue enviado.",
                    status=404,
                )
            rows.append(row)
        return rows

    async def link(self, rows: Sequence[Any], message_id: uuid.UUID) -> None:
        for row in rows:
            await self._attachments.vincular_a_mensaje(row.id, message_id)

    async def attach_history(self, messages: list[Message]) -> list[Message]:
        by_message: dict[uuid.UUID, list[dict[str, Any]]] = {}
        rows = await self._attachments.listar_por_mensajes([m.id for m in messages])
        for row in rows:
            if row.message_id is not None:
                by_message.setdefault(row.message_id, []).append(self._descriptor(row))
        return [
            replace(message, adjuntos=tuple(by_message.get(message.id, ())))
            for message in messages
        ]

    async def context_images(self, messages: Sequence[Message]) -> dict[uuid.UUID, list[ImagePart]]:
        """Base64 images of the recent customer messages, never URLs (SPEC-23 Req. 5)."""
        customer_ids = [m.id for m in messages if m.role is MessageRole.CUSTOMER]
        rows = await self._attachments.listar_por_mensajes(customer_ids)
        by_message: dict[uuid.UUID, list[Any]] = {}
        for row in rows:
            if row.message_id is not None:
                by_message.setdefault(row.message_id, []).append(row)
        images: dict[uuid.UUID, list[ImagePart]] = {}
        total = 0
        for message in reversed(list(messages)):
            if message.role is not MessageRole.CUSTOMER:
                continue
            for row in by_message.get(message.id, []):
                if total >= self._max_images:
                    return images
                try:
                    data = await self._storage.leer(row.storage_key)
                except AttachmentStorageError:
                    logger.warning("Attachment %s could not be read; skipped", row.id)
                    continue
                images.setdefault(message.id, []).append(
                    ImagePart(media_type=row.mime_type, data_base64=base64.b64encode(data).decode())
                )
                total += 1
        return images

    async def cleanup(self) -> int:
        """Delete PENDING rows older than 24 h and files without a row (SPEC-23)."""
        removed = 0
        cutoff = self._clock() - PENDING_TTL
        for row in await self._attachments.listar_pendientes_antes_de(cutoff):
            keys = (row.storage_key, row.thumbnail_key)
            await self._attachments.eliminar(row.id)
            await self._uow.commit()
            await self._safe_delete(*keys)
            removed += 1
        known = await self._attachments.listar_claves()
        for key in await self._storage.listar_claves():
            if key not in known:
                try:
                    await self._storage.eliminar(key)
                    removed += 1
                except AttachmentStorageError:
                    logger.warning("Orphan attachment %s could not be deleted", key)
        return removed

    async def _safe_delete(self, *keys: str) -> None:
        for key in keys:
            try:
                await self._storage.eliminar(key)
            except AttachmentStorageError:
                logger.warning("Attachment file %s could not be deleted", key)
