"""Insert model for `attachment` (SPEC-23).

Lives in shared so the shared persistence adapter never imports a module.
"""
import uuid
from dataclasses import dataclass


@dataclass
class NewAttachment:
    conversation_id: uuid.UUID
    status: str
    storage_key: str
    thumbnail_key: str
    mime_type: str
    size_bytes: int
    width: int
    height: int
