import io

import pillow_heif
from PIL import Image

from app.services.exceptions import UnsupportedFileTypeError

# Registers HEIC/HEIF as a Pillow-openable format (Pillow has no built-in
# decoder for it). Must run in every process that opens uploaded images with
# Pillow — this module is the shared home for that now, imported by both
# card_service.py and the archive-upload worker.
pillow_heif.register_heif_opener()

_READ_CHUNK_SIZE = 65536

# Only used to verify that a file's actual decoded bytes match its declared
# content-type — NOT the source of truth for which types are allowed
# (settings.allowed_card_image_content_types is). A type missing from this
# map simply fails verification (safe default), it never raises a KeyError.
# HEIC and HEIF both decode to Pillow's "HEIF" format name (pillow-heif
# doesn't distinguish the two containers), so both content-types map to it.
EXPECTED_IMAGE_FORMATS = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
    "image/heic": "HEIF",
    "image/heif": "HEIF",
}


def read_limited(file_obj, max_bytes: int) -> bytes:
    """Reads at most ~max_bytes + one chunk before giving up, so an
    oversized upload doesn't get fully buffered into memory before its size
    is checked."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = file_obj.read(_READ_CHUNK_SIZE)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            break
    return b"".join(chunks)


def verify_image_content(data: bytes, content_type: str, filename: str | None) -> None:
    """Confirms `data` actually decodes as an image matching the declared
    content-type, rather than trusting the client-supplied header alone."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            actual_format = img.format
    except Exception:
        raise UnsupportedFileTypeError(f"{filename or 'file'} is not a valid image")

    if actual_format != EXPECTED_IMAGE_FORMATS.get(content_type):
        raise UnsupportedFileTypeError(
            f"{filename or 'file'} content does not match its declared type {content_type}"
        )
