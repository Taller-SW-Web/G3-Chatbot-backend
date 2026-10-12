"""Outbound DTOs: assistant turn result (H3-05).

The streamed text is informational; the persisted assistant message
(`mensajeId` in `fin`) is authoritative (OutputValidator corrects there).
"""
from typing import Any

from pydantic import BaseModel, Field

from src.domain.value_objects.block import BlockType


class ChatBlock(BaseModel):
    id: str = Field(min_length=1)
    tipo: BlockType
    datos: dict[str, Any] = Field(default_factory=dict)


class AssistantTurnResult(BaseModel):
    mensajeId: str
    texto: str = ""
    bloques: list[ChatBlock] = Field(default_factory=list)
    degradado: str | None = None
