"""Customer feedback: reactions and suggestions/bugs (SPEC-24)."""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from src.modules.mod_chat.domain.feedback import NewFeedbackSuggestion
from src.modules.mod_chat.ports.image_processor_port import ImageProcessorPort, InvalidImageError
from src.shared.application.dto.outbox_task_dto import NewOutboxTask
from src.shared.application.ports.attachment_storage_port import (
    AttachmentStorageError,
    AttachmentStoragePort,
)
from src.shared.application.unit_of_work_port import UnitOfWorkPort
from src.shared.domain.caller_identity import CallerIdentity

logger = logging.getLogger(__name__)

REACTIONS = {"POSITIVE", "NEGATIVE"}
SUGGESTION_TYPES = {"SUGGESTION", "BUG"}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TYPE_ALIASES = {
    "SUGERENCIA": "SUGGESTION",
    "SUGGESTION": "SUGGESTION",
    "BUG": "BUG",
    "ERROR": "BUG",
}


class FeedbackError(Exception):
    def __init__(self, code: str, detail: str, *, status: int):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


class FeedbackService:
    def __init__(
        self,
        reactions,
        messages,
        conversations,
        suggestions,
        outbox,
        uow: UnitOfWorkPort,
        *,
        storage: AttachmentStoragePort | None = None,
        processor: ImageProcessorPort | None = None,
        notifier=None,
        send_inline: bool = True,
    ):
        self._reactions = reactions
        self._messages = messages
        self._conversations = conversations
        self._suggestions = suggestions
        self._outbox = outbox
        self._uow = uow
        self._storage = storage
        self._processor = processor
        self._notifier = notifier
        self._send_inline = send_inline

    async def react(
        self, caller: CallerIdentity | None, message_id: uuid.UUID, reaction: str
    ) -> dict[str, Any]:
        normalized = (reaction or "").strip().upper()
        if normalized not in REACTIONS:
            raise FeedbackError("VALIDACION", "La reacción debe ser POSITIVE o NEGATIVE.", status=400)
        if caller is None:
            raise FeedbackError("RECURSO_NO_ENCONTRADO", "Mensaje no encontrado.", status=404)
        message = await self._messages.obtener_por_id(message_id)
        if message is None or await self._conversations.find_owned(message.conversation_id, caller) is None:
            raise FeedbackError("RECURSO_NO_ENCONTRADO", "Mensaje no encontrado.", status=404)
        await self._reactions.upsert(
            message_id,
            customer_id=caller.customer_id,
            anonymous_sid=caller.anonymous_sid,
            reaction=normalized,
        )
        await self._uow.commit()
        return {"mensajeId": str(message_id), "reaccion": normalized}

    async def create_suggestion(
        self,
        customer_id: uuid.UUID,
        *,
        kind: str,
        title: str,
        description: str,
        contact_email: str,
        idempotency_key: str,
        screenshot: bytes | None = None,
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise FeedbackError("VALIDACION", "Idempotency-Key es obligatorio.", status=400)
        existing = await self._suggestions.obtener_por_idempotency_key(idempotency_key)
        if existing is not None:
            return self._answer(existing)

        normalized_type = _TYPE_ALIASES.get((kind or "").strip().upper())
        title = (title or "").strip()
        description = (description or "").strip()
        contact_email = (contact_email or "").strip()
        if normalized_type is None:
            raise FeedbackError("VALIDACION", "El tipo debe ser SUGGESTION o BUG.", status=400)
        if not 3 <= len(title) <= 120:
            raise FeedbackError("VALIDACION", "El título debe tener entre 3 y 120 caracteres.", status=400)
        if not 6 <= len(description) <= 1000:
            raise FeedbackError(
                "VALIDACION", "La descripción debe tener entre 6 y 1000 caracteres.", status=400
            )
        if not _EMAIL.fullmatch(contact_email):
            raise FeedbackError("VALIDACION", "El correo de contacto no es válido.", status=400)

        screenshot_key = await self._save_screenshot(screenshot)
        try:
            row = await self._suggestions.guardar(
                NewFeedbackSuggestion(
                    customer_id=customer_id,
                    type=normalized_type,
                    title=title,
                    description=description,
                    contact_email=contact_email,
                    screenshot_key=screenshot_key,
                    idempotency_key=idempotency_key,
                )
            )
            payload = {
                "suggestion_id": str(row.id),
                "tipo": normalized_type,
                "titulo": title,
                "descripcion": description,
                "correo": contact_email,
                "captura": screenshot_key,
                "capturaUrl": self._screenshot_url(screenshot_key),
            }
            await self._outbox.guardar(NewOutboxTask(type="SEND_FEEDBACK_EMAIL", payload=payload))
            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            if screenshot_key is not None and self._storage is not None:
                try:
                    await self._storage.eliminar(screenshot_key)
                except AttachmentStorageError:
                    logger.warning("Screenshot %s could not be removed after failure", screenshot_key)
            raise

        if self._send_inline and self._notifier is not None:
            try:
                await self._notifier.send(payload)
                await self._suggestions.marcar_notificacion(row.id, "SENT")
                await self._uow.commit()
            except Exception as exc:
                logger.warning("Internal feedback notification failed; outbox will retry: %s", type(exc).__name__)
        return self._answer(row)

    async def _save_screenshot(self, screenshot: bytes | None) -> str | None:
        if not screenshot:
            return None
        if self._processor is None or self._storage is None:
            raise FeedbackError(
                "SERVICIO_NO_DISPONIBLE", "No pudimos guardar la captura.", status=503
            )
        try:
            processed = self._processor.procesar(screenshot)
        except InvalidImageError as exc:
            raise FeedbackError("ADJUNTO_INVALIDO", str(exc), status=400) from exc
        if processed.mime_type not in {"image/jpeg", "image/png"}:
            raise FeedbackError(
                "ADJUNTO_INVALIDO", "La captura debe ser JPG o PNG.", status=400
            )
        suffix = "jpg" if processed.mime_type == "image/jpeg" else "png"
        key = f"feedback/{uuid.uuid4().hex}.{suffix}"
        try:
            await self._storage.guardar(key, processed.data, processed.mime_type)
        except AttachmentStorageError as exc:
            raise FeedbackError(
                "SERVICIO_NO_DISPONIBLE", "No pudimos guardar la captura.", status=503
            ) from exc
        return key

    def _screenshot_url(self, key: str | None) -> str | None:
        if key is None or self._storage is None:
            return None
        try:
            return self._storage.url_firmada(key, 7 * 24 * 3600).url
        except Exception:
            return None

    @staticmethod
    def _answer(row) -> dict[str, Any]:
        return {
            "sugerenciaId": str(row.id),
            "codigoSeguimiento": f"SUG-{str(row.id).replace('-', '')[:8].upper()}",
        }
