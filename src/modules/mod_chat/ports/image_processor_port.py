"""Port: validate, normalize and thumbnail chat images (SPEC-23)."""
from dataclasses import dataclass
from typing import Protocol


class InvalidImageError(Exception):
    """The file is not a supported image or exceeds the allowed dimensions."""


@dataclass(frozen=True)
class ProcessedImage:
    mime_type: str
    data: bytes
    thumbnail: bytes
    width: int
    height: int


class ImageProcessorPort(Protocol):
    def procesar(self, data: bytes) -> ProcessedImage:
        """Detect by binary signature, strip EXIF, bound dimensions and thumbnail."""
        ...
