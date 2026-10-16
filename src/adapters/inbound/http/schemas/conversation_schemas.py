"""Wire format of the conversation endpoints (contratos-integracion.md 2.1, camelCase Spanish names).

List and history are bare JSON arrays with the shape the frontend ChatbotApiPort already declares
(`ConversacionResumen`, `Mensaje`).
"""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from src.domain.entities.conversation import ConversationSummary, HighlightSegment
from src.domain.entities.message import Message, MessageRole


class ConversationCreatedOut(BaseModel):
    conversacionId: uuid.UUID


class ConversationSummaryOut(BaseModel):
    id: uuid.UUID
    titulo: str
    preview: str
    ultimoMensajeEn: datetime

    @classmethod
    def from_domain(cls, c: ConversationSummary) -> "ConversationSummaryOut":
        return cls(id=c.id, titulo=c.title, preview=c.preview, ultimoMensajeEn=c.last_message_at)


class HighlightSegmentOut(BaseModel):
    texto: str
    coincide: bool

    @classmethod
    def from_domain(cls, s: HighlightSegment) -> "HighlightSegmentOut":
        return cls(texto=s.text, coincide=s.match)


class SearchHighlightOut(BaseModel):
    titulo: list[HighlightSegmentOut]
    fragmento: list[HighlightSegmentOut] | None  # null when only the title matched


class ConversationSearchResultOut(ConversationSummaryOut):
    """Search row: the list shape plus `resaltado` (SPEC-05 Req. 1 · Buscar chats). Segments are plain
    text; the client marks the ones with `coincide` and must not render them as HTML."""

    resaltado: SearchHighlightOut | None = None

    @classmethod
    def from_domain(cls, c: ConversationSummary) -> "ConversationSearchResultOut":
        base = ConversationSummaryOut.from_domain(c)
        highlight = None
        if c.highlight is not None:
            highlight = SearchHighlightOut(
                titulo=[HighlightSegmentOut.from_domain(s) for s in c.highlight.title],
                fragmento=(
                    [HighlightSegmentOut.from_domain(s) for s in c.highlight.snippet]
                    if c.highlight.snippet is not None else None
                ),
            )
        return cls(**base.model_dump(), resaltado=highlight)


class MessageOut(BaseModel):
    id: uuid.UUID
    conversacionId: uuid.UUID
    rol: str  # "user" | "bot", as in the frontend `MensajeRol`
    texto: str
    bloques: Any | None = None
    creadoEn: datetime

    @classmethod
    def from_domain(cls, m: Message) -> "MessageOut":
        rol = "user" if m.role is MessageRole.CUSTOMER else "bot"
        return cls(
            id=m.id, conversacionId=m.conversation_id, rol=rol, texto=m.text,
            bloques=m.blocks, creadoEn=m.created_at,
        )
