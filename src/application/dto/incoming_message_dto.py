"""Inbound DTOs: POST /chat/conversaciones/{id}/mensajes (ADR-0021).

One body, three shapes: `texto` (+ optional `adjuntoIds`, SPEC-23),
or a direct `accion` answered by the ActionDispatcher without the LLM.
Unknown fields are rejected with 400 before the stream opens.
"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_TEXT_LENGTH = 1000
MAX_ATTACHMENTS = 3


class ToolAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tipo: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    texto: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT_LENGTH)
    adjuntoIds: list[str] | None = Field(default=None, max_length=MAX_ATTACHMENTS)
    accion: ToolAction | None = None

    @model_validator(mode="after")
    def _one_shape(self) -> "SendMessageRequest":
        has_text = self.texto is not None or self.adjuntoIds is not None
        has_action = self.accion is not None
        if has_text == has_action:
            raise ValueError("body must carry texto/adjuntoIds or accion, not both nor neither")
        return self


class AcceptedResponse(BaseModel):
    mensajeId: str


class StreamError(BaseModel):
    code: str


SseEventName = Literal["aceptado", "token", "bloque", "degradado", "fin", "error"]
