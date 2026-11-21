"""Pillow-backed image processor (SPEC-23).

The accepted type comes from the binary signature, never from the extension or
the client `Content-Type`. Every image is re-encoded, which drops EXIF/GPS,
then bounded to 2048 px and thumbnailed to 320 px.
"""
from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from src.modules.mod_chat.ports.image_processor_port import (
    ImageProcessorPort,
    InvalidImageError,
    ProcessedImage,
)

MAX_SIZE_BYTES = 5 * 1024 * 1024
_ALLOWED = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"), "WEBP": ("image/webp", "webp")}


def _detect_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class PillowImageProcessor(ImageProcessorPort):
    def __init__(
        self,
        *,
        max_bytes: int = MAX_SIZE_BYTES,
        max_side: int = 4096,
        stored_side: int = 2048,
        thumbnail_side: int = 320,
    ):
        self._max_bytes = max_bytes
        self._max_side = max_side
        self._stored_side = stored_side
        self._thumbnail_side = thumbnail_side

    def procesar(self, data: bytes) -> ProcessedImage:
        if not data or len(data) > self._max_bytes:
            raise InvalidImageError("El archivo debe pesar entre 1 byte y 5 MB.")
        mime = _detect_mime(data)
        if mime is None:
            raise InvalidImageError("Solo se aceptan imágenes JPG, PNG o WebP.")
        try:
            with Image.open(BytesIO(data)) as probe:
                probe.verify()
            with Image.open(BytesIO(data)) as image:
                fmt = image.format
                if fmt not in _ALLOWED or _ALLOWED[fmt][0] != mime:
                    raise InvalidImageError("Solo se aceptan imágenes JPG, PNG o WebP.")
                if max(image.size) > self._max_side:
                    raise InvalidImageError("La imagen supera las dimensiones permitidas.")
                normalized = ImageOps.exif_transpose(image)
                has_alpha = normalized.mode in ("RGBA", "LA") or "transparency" in normalized.info
                normalized = normalized.convert("RGBA" if has_alpha else "RGB")
                normalized.thumbnail((self._stored_side, self._stored_side), Image.Resampling.LANCZOS)
                stored = self._encode(normalized, fmt)
                width, height = normalized.size
                thumb = normalized.copy()
                thumb.thumbnail((self._thumbnail_side, self._thumbnail_side), Image.Resampling.LANCZOS)
                thumbnail = self._encode(thumb, fmt)
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            if isinstance(exc, InvalidImageError):
                raise
            raise InvalidImageError("No pudimos procesar ese archivo.") from exc
        return ProcessedImage(
            mime_type=mime,
            data=stored,
            thumbnail=thumbnail,
            width=width,
            height=height,
        )

    @staticmethod
    def _encode(image: Image.Image, fmt: str) -> bytes:
        out = BytesIO()
        if fmt == "JPEG":
            image.save(out, format="JPEG", quality=85, optimize=True)
        elif fmt == "PNG":
            image.save(out, format="PNG", optimize=True)
        else:
            image.save(out, format="WEBP", quality=85)
        return out.getvalue()
