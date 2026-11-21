"""Pillow image processor: signature, EXIF stripping, dimensions and thumbnail (SPEC-23)."""
from io import BytesIO

import pytest
from PIL import Image

from src.modules.mod_chat.adapters.pillow_image_processor import PillowImageProcessor
from src.modules.mod_chat.ports.image_processor_port import InvalidImageError


def png_bytes(size=(800, 600), color=(10, 20, 30)) -> bytes:
    image = Image.new("RGB", size, color)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def jpeg_with_exif(size=(800, 600)) -> bytes:
    image = Image.new("RGB", size, (200, 30, 40))
    exif = Image.Exif()
    exif[0x010F] = "TestCamera"
    exif[0x0112] = 6  # rotate 90 degrees
    out = BytesIO()
    image.save(out, format="JPEG", exif=exif)
    return out.getvalue()


def test_png_is_normalized_and_thumbnailed():
    processed = PillowImageProcessor().procesar(png_bytes((1200, 900)))
    assert processed.mime_type == "image/png"
    assert processed.width == 1200 and processed.height == 900
    thumb = Image.open(BytesIO(processed.thumbnail))
    assert max(thumb.size) <= 320


def test_jpeg_reencoding_drops_exif_and_honors_orientation():
    processed = PillowImageProcessor().procesar(jpeg_with_exif((900, 600)))
    image = Image.open(BytesIO(processed.data))
    assert image.getexif() == {}
    # Orientation 6 means exif_transpose swaps width/height.
    assert (processed.width, processed.height) == (600, 900)


def test_oversized_dimensions_are_rejected():
    with pytest.raises(InvalidImageError):
        PillowImageProcessor(max_side=100).procesar(png_bytes((200, 50)))


def test_non_image_content_is_rejected_even_with_image_extension():
    with pytest.raises(InvalidImageError):
        PillowImageProcessor().procesar(b"MZ\x90\x00this is an executable")


def test_empty_and_too_large_payloads_are_rejected():
    with pytest.raises(InvalidImageError):
        PillowImageProcessor(max_bytes=10).procesar(png_bytes())
